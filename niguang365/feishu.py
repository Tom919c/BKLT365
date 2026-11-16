"""从飞书多维表格拉取收集表提交的图片。

默认使用 ``OfficialFeishuBase``：通过 ``lark-cli`` 调用飞书官方多维表格接口，
当前用户必须具备表格阅读权限。2026-11-15 已实测记录读取和附件下载。

旧的 ``FeishuBase`` 保留作备用：它复用 Chrome 登录态调用网页内部接口，
但内部接口可能失效（2026-11-15 实测返回 404），不再作为默认路径。

字段名映射：显式字段 ID 优先；自动推断按文本结构识别，不依赖列顺序。
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests
import websockets

DEFAULT_HOST = "https://uestc-bklt.feishu.cn"
DEFAULT_PORT = 9222

RECORDS_PATH = "/space/api/v1/bitable/{app_token}/records"
DOWNLOAD_PATH = "/space/api/box/stream/download/all/{file_token}"

_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class FeishuError(RuntimeError):
    """飞书取数/下载失败。"""


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass
class Attachment:
    token: str
    name: str
    size: int = 0
    mime: str = ""
    width: int = 0
    height: int = 0
    record_id: str = ""
    field_id: str = ""

    @property
    def safe_name(self) -> str:
        cleaned = _UNSAFE_FILENAME.sub("_", self.name).strip() or f"{self.token}.bin"
        return cleaned

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/") or self.safe_name.lower().endswith(
            (".jpg", ".jpeg", ".png", ".heic", ".webp", ".bmp", ".tif", ".tiff")
        )


@dataclass
class FeishuRecord:
    record_id: str
    texts: dict[str, str] = field(default_factory=dict)
    attachments: list[Attachment] = field(default_factory=list)
    submitted_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def text_values(self) -> list[str]:
        return [v for v in self.texts.values() if v]

    def pick_name(self, field_id: str | None = None) -> str:
        """猜「姓名」字段。

        优先用显式指定的 field_id；否则取**含中日韩字符**的文本字段
        （姓名一般是中文，而学号/手机号/编号是纯数字）。
        """
        if field_id and self.texts.get(field_id):
            return self.texts[field_id]
        for value in self.texts.values():
            if value and _CJK.search(value):
                return value
        for value in self.texts.values():
            if value and not value.isdigit():
                return value
        return ""

    def pick_title(self, name: str = "", field_id: str | None = None) -> str:
        """猜「图名」字段。表单里没有这个字段时返回空串。

        排除掉姓名本身、纯数字（学号/编号）、纯日期。
        """
        if field_id and self.texts.get(field_id):
            return self.texts[field_id]
        for value in self.texts.values():
            value = (value or "").strip()
            if not value or value == name:
                continue
            if value.isdigit():
                continue
            if _DATE_ONLY.fullmatch(value):
                continue
            return value
        return ""


_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_DATE_ONLY = re.compile(r"^\d{4}[-/年]?\d{1,2}[-/月]?\d{1,2}日?$")


def _dedupe(path: Path) -> Path:
    """目标已存在时追加 _1 / _2 ..."""
    if not path.exists():
        return path
    stem, suffix, n = path.stem, path.suffix, 1
    while path.exists():
        path = path.with_name(f"{stem}_{n}{suffix}")
        n += 1
    return path


@dataclass
class SyncReport:
    dest_dir: Path
    downloaded: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.downloaded)


# --------------------------------------------------------------------------
# CDP 小工具
# --------------------------------------------------------------------------


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _list_targets(port: int) -> list[dict]:
    with _opener().open(f"http://127.0.0.1:{port}/json", timeout=5) as r:
        return json.loads(r.read().decode())


def _new_tab(port: int, url: str) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}",
        method="PUT",
    )
    with _opener().open(req, timeout=10) as r:
        return json.loads(r.read().decode())


async def _call(ws_url: str, method: str, params: dict | None = None, timeout=60):
    async with websockets.connect(ws_url, max_size=200 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("id") == 1:
                if "error" in msg:
                    raise FeishuError(f"{method} 失败：{msg['error']}")
                return msg.get("result", {})


def _evaluate(ws_url: str, expression: str):
    result = asyncio.run(
        _call(
            ws_url,
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=90,
        )
    )
    if "exceptionDetails" in result:
        raise FeishuError(f"页面脚本报错：{result['exceptionDetails']}")
    return result.get("result", {}).get("value")


def _cookies(ws_url: str) -> dict[str, str]:
    result = asyncio.run(
        _call(ws_url, "Network.getAllCookies", {}, timeout=30)
    )
    return {
        c["name"]: c.get("value") or ""
        for c in result.get("cookies", [])
        if "feishu" in c.get("domain", "") or "lark" in c.get("domain", "")
    }


# --------------------------------------------------------------------------
# 主类
# --------------------------------------------------------------------------


class FeishuBase:
    """读取飞书多维表格并下载附件。"""

    def __init__(
        self,
        app_token: str,
        table_id: str,
        *,
        view_id: str | None = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        wiki_token: str | None = None,
    ):
        self.app_token = app_token
        self.table_id = table_id
        self.view_id = view_id
        self.host = host.rstrip("/")
        self.port = port
        self.wiki_token = wiki_token
        self._ws_url: str | None = None

    # -- 页面 ---------------------------------------------------------------

    def page_ws(self) -> str:
        """找一个已登录的飞书页面；没有就开一个。"""
        if self._ws_url:
            return self._ws_url

        host_key = urllib.parse.urlparse(self.host).netloc
        for t in _list_targets(self.port):
            if t.get("type") == "page" and host_key in t.get("url", ""):
                self._ws_url = t["webSocketDebuggerUrl"]
                return self._ws_url

        # 没有就开一个（wiki 页能同时拿到 app_token 与 cookie）
        url = (
            f"{self.host}/wiki/{self.wiki_token}"
            if self.wiki_token
            else f"{self.host}/base/{self.app_token}"
        )
        tab = _new_tab(self.port, url)
        self._ws_url = tab["webSocketDebuggerUrl"]
        return self._ws_url

    def _fetch_text(self, path: str) -> str:
        ws = self.page_ws()
        expr = (
            "(async () => {"
            f"  const r = await fetch({json.dumps(path)}, {{credentials:'include'}});"
            "  return await r.text();"
            "})()"
        )
        value = _evaluate(ws, expr)
        if value is None:
            raise FeishuError(f"请求无返回：{path}")
        return value

    # -- 记录 ---------------------------------------------------------------

    def list_records(self) -> list[FeishuRecord]:
        query = {"tableId": self.table_id}
        if self.view_id:
            query["viewId"] = self.view_id
        path = RECORDS_PATH.format(app_token=self.app_token)
        path += "?" + urllib.parse.urlencode(query)

        raw = self._fetch_text(path)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FeishuError(f"记录接口返回非 JSON：{raw[:200]}") from exc

        if payload.get("code") != 0:
            raise FeishuError(
                f"记录接口报错 code={payload.get('code')} msg={payload.get('msg')}"
            )

        data = payload.get("data") or {}
        blob = data.get("records")
        if isinstance(blob, str):
            obj = _decode_records(blob)
        elif isinstance(blob, dict):
            obj = blob
        else:
            raise FeishuError("记录接口未返回 records 字段")

        return _parse_records(obj)

    # -- 下载 ---------------------------------------------------------------

    def download(self, attachment: Attachment, dest_dir: Path) -> Path:
        """下载单个附件到 dest_dir，返回落地路径。"""
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / attachment.safe_name
        if target.exists() and target.stat().st_size == attachment.size:
            return target

        ws = self.page_ws()
        cookies = _cookies(ws)
        url = self.host + DOWNLOAD_PATH.format(file_token=attachment.token)

        try:
            resp = requests.get(url, cookies=cookies, timeout=180, stream=True)
        except requests.RequestException as exc:
            raise FeishuError(f"下载失败 {attachment.name}：{exc}") from exc

        if resp.status_code != 200:
            raise FeishuError(
                f"下载 {attachment.name} 返回 HTTP {resp.status_code}"
            )

        tmp = target.with_suffix(target.suffix + ".part")
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    fh.write(chunk)
        tmp.replace(target)
        return target

    def sync(
        self,
        root: str | Path,
        *,
        inbox_dirname: str = "收件箱",
        on_date: date | None = None,
        images_only: bool = True,
        rename: str = "keep",
        name_field_id: str | None = None,
        title_field_id: str | None = None,
    ) -> SyncReport:
        """把表格里所有附件下载到 ``<root>/<收件箱>/<YYYYMMDD>/``。

        重复同步时，最终命名目标已存在且大小一致的文件会直接跳过，不再生成
        ``_1`` / ``_2`` 副本；同名但大小不同会跳过并报告冲突，不覆盖本地文件。

        Args:
            rename: ``keep`` 保留原文件名；``convention`` 重命名为
                ``YYYYMMDD_姓名_图名.ext``；表里没有图名字段时退回
                ``YYYYMMDD_姓名_NN.ext``。
            name_field_id: 显式指定「姓名」字段 ID，不填则自动推断。
            title_field_id: 显式指定「图名」字段 ID，不填则自动推断。
        """
        root = Path(root)
        records = self.list_records()
        report = SyncReport(dest_dir=root / inbox_dirname)

        for record in records:
            day = (record.submitted_at or datetime.now()).date()
            if on_date and record.submitted_at is None:
                day = on_date
            day_dir = root / inbox_dirname / day.strftime("%Y%m%d")
            report.dest_dir = day_dir

            name = record.pick_name(name_field_id)
            title = record.pick_title(name, title_field_id)
            seq = 0

            for att in record.attachments:
                if images_only and not att.is_image:
                    report.skipped.append((att.name, "非图片"))
                    continue
                seq += 1

                # 先按最终命名算出目标。目标已存在就跳过，绝不生成 _1 副本。
                if rename == "convention" and name:
                    if not title:
                        tail = f"{seq:02d}"
                    elif seq == 1:
                        tail = title
                    else:
                        tail = f"{title}{seq:02d}"
                    target = day_dir / (
                        f"{day:%Y%m%d}_{name}_{tail}"
                        f"{Path(att.safe_name).suffix.lower()}"
                    )
                else:
                    target = day_dir / att.safe_name

                if target.exists():
                    actual_size = target.stat().st_size
                    if att.size and actual_size == att.size:
                        report.skipped.append(
                            (att.name, f"已存在，跳过：{target.name}")
                        )
                    else:
                        report.skipped.append(
                            (
                                att.name,
                                "同名文件已存在但大小不同，未覆盖："
                                f"{target.name}（表格 {att.size}，本地 {actual_size}）",
                            )
                        )
                    continue

                try:
                    path = self.download(att, day_dir)
                except FeishuError as exc:
                    report.skipped.append((att.name, str(exc)))
                    continue

                if rename == "convention" and name:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    path.replace(target)
                    path = target

                report.downloaded.append((att.name, str(path)))

        return report


# --------------------------------------------------------------------------
# 官方 OpenAPI（lark-cli）
# --------------------------------------------------------------------------


class OfficialFeishuBase(FeishuBase):
    """通过 ``lark-cli`` 调用官方多维表格接口。

    使用当前用户身份，不读取或保存用户 token。附件先下载到 lark-cli
    明确允许的本机临时目录，再移动到项目收件箱。
    """

    def __init__(
        self,
        app_token: str,
        table_id: str,
        *,
        cli_path: str = "lark-cli",
        timeout: int = 180,
        **kwargs,
    ):
        super().__init__(app_token, table_id, **kwargs)
        self.cli_path = cli_path
        self.timeout = timeout

    def _run(self, arguments: list[str], *, timeout: int | None = None) -> dict:
        cmd = [self.cli_path, "base", *arguments, "--as", "user"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise FeishuError(
                "找不到 lark-cli，请先安装并确保当前飞书账号已连接"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise FeishuError(f"飞书官方接口调用超时：{cmd[2]}") from exc

        try:
            payload = json.loads(proc.stdout or "")
        except json.JSONDecodeError as exc:
            detail = (proc.stderr or proc.stdout or "")[:300]
            raise FeishuError(f"飞书官方接口返回非 JSON：{detail}") from exc

        if proc.returncode != 0 or not payload.get("ok", False):
            error = payload.get("error") or {}
            detail = error.get("message") or proc.stderr or proc.stdout
            raise FeishuError(f"飞书官方接口调用失败：{detail}")
        return payload

    def list_records(self) -> list[FeishuRecord]:
        records: list[FeishuRecord] = []
        offset = 0
        while True:
            arguments = [
                "+record-list",
                "--base-token", self.app_token,
                "--table-id", self.table_id,
                "--format", "json",
                "--limit", "200",
                "--offset", str(offset),
            ]
            if self.view_id:
                arguments.extend(["--view-id", self.view_id])
            payload = self._run(arguments)
            data = payload.get("data") or {}
            page = _parse_official_records(data)
            records.extend(page)

            rows = data.get("data") or []
            if not data.get("has_more"):
                break
            if not rows:
                raise FeishuError("飞书记录分页异常：has_more=true 但本页无记录")
            offset += len(rows)
        return records

    def download(self, attachment: Attachment, dest_dir: Path) -> Path:
        """用官方附件接口下载单个文件，再移动到目标目录。"""
        if not attachment.record_id:
            raise FeishuError(f"附件缺少 record_id：{attachment.name}")

        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / attachment.safe_name
        if target.exists() and target.stat().st_size == attachment.size:
            return target
        if target.exists():
            # 同名但大小不同时不覆盖用户已有文件。
            target = _dedupe(target)

        local_app_data = os.environ.get("LOCALAPPDATA")
        temp_parent = (
            Path(local_app_data) / "Temp"
            if local_app_data
            else Path(tempfile.gettempdir())
        )
        with tempfile.TemporaryDirectory(
            prefix="niguang365-feishu-", dir=temp_parent
        ) as tmp:
            payload = self._run(
                [
                    "+record-download-attachment",
                    "--base-token", self.app_token,
                    "--table-id", self.table_id,
                    "--record-id", attachment.record_id,
                    "--file-token", attachment.token,
                    "--output", tmp,
                ],
                timeout=max(self.timeout, 300),
            )
            items = (payload.get("data") or {}).get("downloaded") or []
            item = next(
                (x for x in items if x.get("file_token") == attachment.token),
                None,
            )
            if not item or not item.get("saved_path"):
                raise FeishuError(f"官方附件接口未返回下载文件：{attachment.name}")

            tmp_root = Path(tmp).resolve()
            saved = Path(item["saved_path"]).resolve()
            try:
                saved.relative_to(tmp_root)
            except ValueError as exc:
                raise FeishuError("附件返回路径超出本次临时目录") from exc
            if not saved.is_file():
                raise FeishuError(f"附件下载后不存在：{attachment.name}")
            if attachment.size and saved.stat().st_size != attachment.size:
                raise FeishuError(
                    f"附件大小不一致：{attachment.name}，"
                    f"表格 {attachment.size}，实际 {saved.stat().st_size}"
                )
            shutil.move(str(saved), str(target))
        return target


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def _parse_official_datetime(value, field_name: str) -> datetime | None:
    """解析官方记录中的日期字段；普通数字或文本不当作时间。"""
    if not any(word in field_name for word in ("日期", "时间")):
        return None
    if isinstance(value, (int, float)):
        if value > 1e12:
            return datetime.fromtimestamp(value / 1000)
        if value > 1e9:
            return datetime.fromtimestamp(value)
        return None
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _parse_official_records(data: dict) -> list[FeishuRecord]:
    """把官方 JSON 行矩阵转换为现有 FeishuRecord 模型。"""
    fields = data.get("fields") or []
    field_ids = data.get("field_id_list") or fields
    rows = data.get("data") or []
    record_ids = data.get("record_id_list") or []
    if not (len(fields) == len(field_ids) and len(rows) == len(record_ids)):
        raise FeishuError("飞书官方记录返回的字段或记录数量不一致")

    out: list[FeishuRecord] = []
    for record_id, row in zip(record_ids, rows):
        if len(row) != len(fields):
            raise FeishuError(f"飞书记录 {record_id} 的字段列数不一致")
        rec = FeishuRecord(
            record_id=record_id,
            raw=dict(zip(fields, row)),
        )
        for field_name, field_id, value in zip(fields, field_ids, row):
            if (
                isinstance(value, list)
                and value
                and isinstance(value[0], dict)
                and "file_token" in value[0]
            ):
                for item in value:
                    rec.attachments.append(
                        Attachment(
                            token=item.get("file_token", ""),
                            name=item.get("name", ""),
                            size=int(item.get("size") or 0),
                            mime=item.get("mime_type") or item.get("mimeType", ""),
                            width=int(item.get("width") or 0),
                            height=int(item.get("height") or 0),
                            record_id=record_id,
                            field_id=field_id,
                        )
                    )
                continue

            submitted_at = _parse_official_datetime(value, field_name)
            if submitted_at is not None:
                rec.submitted_at = submitted_at
            elif isinstance(value, str):
                rec.texts[field_id] = value.strip()
            elif (
                isinstance(value, list)
                and value
                and isinstance(value[0], dict)
                and "text" in value[0]
            ):
                rec.texts[field_id] = " ".join(
                    str(item.get("text", "")) for item in value
                ).strip()
            elif isinstance(value, (int, float)):
                rec.texts[field_id] = str(value)
        out.append(rec)
    return out


def _decode_records(blob: str) -> dict:
    """records 字段是 base64(gzip(json))，解开它。"""
    try:
        raw = base64.b64decode(blob)
    except Exception as exc:  # noqa: BLE001
        raise FeishuError(f"records base64 解码失败：{exc}") from exc

    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            raise FeishuError(f"records gzip 解压失败：{exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FeishuError(f"records JSON 解析失败：{exc}") from exc


def _parse_records(obj: dict) -> list[FeishuRecord]:
    record_map: dict[str, dict] = obj.get("recordMap") or {}
    out: list[FeishuRecord] = []

    for record_id, fields in record_map.items():
        rec = FeishuRecord(record_id=record_id, raw=fields)
        for field_id, cell in (fields or {}).items():
            value = cell.get("value") if isinstance(cell, dict) else cell

            if isinstance(value, list) and value and isinstance(value[0], dict):
                if "attachmentToken" in value[0]:
                    for item in value:
                        rec.attachments.append(
                            Attachment(
                                token=item.get("attachmentToken") or item.get("id", ""),
                                name=item.get("name", ""),
                                size=int(item.get("size") or 0),
                                mime=item.get("mimeType", ""),
                                width=int(item.get("width") or 0),
                                height=int(item.get("height") or 0),
                                record_id=record_id,
                                field_id=field_id,
                            )
                        )
                elif "text" in value[0]:
                    rec.texts[field_id] = " ".join(
                        str(i.get("text", "")) for i in value
                    ).strip()
            elif isinstance(value, (int, float)) and value > 1e12:
                # 毫秒时间戳
                rec.submitted_at = datetime.fromtimestamp(value / 1000)

        out.append(rec)

    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="飞书多维表格附件下载")
    ap.add_argument("--app-token", required=True)
    ap.add_argument("--table-id", required=True)
    ap.add_argument("--view-id")
    ap.add_argument("--wiki-token")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--cli-path", default="lark-cli", help="lark-cli 可执行文件")
    ap.add_argument("--root", default=r"D:\逆光365图片下载")
    ap.add_argument("--list-only", action="store_true", help="只列出，不下载")
    ap.add_argument(
        "--rename",
        choices=("keep", "convention"),
        default="keep",
        help="convention = YYYYMMDD_姓名_NN.ext",
    )
    ap.add_argument("--name-field-id", help="显式指定姓名字段 ID")
    ap.add_argument("--title-field-id", help="显式指定图名字段 ID")
    args = ap.parse_args()

    base = OfficialFeishuBase(
        args.app_token,
        args.table_id,
        view_id=args.view_id,
        host=args.host,
        port=args.port,
        wiki_token=args.wiki_token,
        cli_path=args.cli_path,
    )

    records = base.list_records()
    print(f"共 {len(records)} 条记录\n")
    for rec in records:
        texts = " / ".join(rec.text_values()) or "(无文本字段)"
        when = rec.submitted_at.strftime("%Y-%m-%d %H:%M") if rec.submitted_at else "?"
        name = rec.pick_name(args.name_field_id)
        title = rec.pick_title(name, args.title_field_id)
        print(
            f"[{rec.record_id}] {when}  姓名={name or '?'}  图名={title or '(缺)'}"
        )
        print(f"    文本字段: {texts}")
        for att in rec.attachments:
            flag = "图片" if att.is_image else "其他"
            print(f"    - {att.name}  {att.size/1024:.0f} KB  {flag}  {att.token}")

    if args.list_only:
        return 0

    report = base.sync(
        args.root,
        rename=args.rename,
        name_field_id=args.name_field_id,
        title_field_id=args.title_field_id,
    )
    print(f"\n下载 {report.count} 个到 {report.dest_dir}")
    for name, path in report.downloaded:
        print(f"  ✓ {name}")
    for name, why in report.skipped:
        print(f"  ✗ {name}  ({why})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
