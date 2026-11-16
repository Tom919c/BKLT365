"""逆光365 命令行入口。

    python -m niguang365 check-login
    python -m niguang365 scan      --date 20260915
    python -m niguang365 archive   --date 20260915
    python -m niguang365 dry-run   --date 20260915
    python -m niguang365 publish   --date 20260915            # 预览，不点发布
    python -m niguang365 publish   --date 20260915 --auto      # 真发布
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from .collect import CollectError, archive, scan_inbox
from .compose import ComposeError, build_content
from .naming import FilenameError
from .publish import PublishError, check_login, publish_note

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

DEFAULTS = {
    "root": r"D:\逆光365",
    "repo": r"D:\Code\VSCode\BKLT365\XiaohongshuSkills",
    "python": r"C:\Users\chenquan\.workbuddy-ai\binaries\python\envs\xhs\Scripts\python.exe",
    "account": None,
    "roster": None,
    "tags": ["#电子科技大学", "#逆光日记"],
    "preview": True,
    "post_time": None,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.is_file():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def parse_date(text: str | None) -> date:
    if not text:
        return date.today()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise SystemExit(f"日期格式无法识别：{text}（试试 20260915 或 2026-09-15）")


def _fmt_candidates(cands) -> str:
    lines = []
    for c in cands:
        flag = "★入选" if c.selected else "  ——  "
        extra = f"  @{c.handle}" if c.handle else "  @缺号"
        warn = f"  ⚠ {'; '.join(c.problems)}" if c.problems else ""
        lines.append(f"{flag}  {c.info.photographer}/{c.info.title}{extra}{warn}")
    return "\n".join(lines) or "（无候选）"


def _report_unparsed(scan) -> None:
    """报告无法解析的文件 —— 这些文件会被忽略，必须让人看见。"""
    if not scan.unparsed:
        return
    print(f"\n⚠ 有 {len(scan.unparsed)} 个文件不符合命名规范，已忽略：")
    for path, why in scan.unparsed:
        print(f"   {path.name}")
        print(f"      {why}")


# --------------------------------------------------------------------------


def cmd_check_login(args, cfg) -> int:
    result = check_login(repo=cfg["repo"], python=cfg["python"])
    print(result["raw"])
    if not result["logged_in"]:
        print("\n请在 Chrome 窗口扫码登录，或用 `open-login` 打开登录页。")
    return 0 if result["logged_in"] else 1


def cmd_open_login(args, cfg) -> int:
    from .logincheck import open_login_page

    status = open_login_page(args.port)
    print(status.describe())
    if status.reachable and not status.logged_in:
        print("\nChrome 已停在登录页，请扫码。扫完再跑 check-login 确认。")
    return 0 if status.reachable else 2


def cmd_fetch(args, cfg) -> int:
    """从飞书多维表格下载收集到的图片。"""
    from .feishu import FeishuBase, FeishuError, OfficialFeishuBase

    fc = cfg.get("feishu") or {}
    if not fc.get("app_token") or not fc.get("table_id"):
        print("配置里缺少 feishu.app_token / feishu.table_id", file=sys.stderr)
        return 2

    backend = fc.get("backend") or "official"
    common = {
        "view_id": fc.get("view_id"),
        "host": fc.get("host") or "https://uestc-bklt.feishu.cn",
        "port": int(fc.get("cdp_port") or 9222),
        "wiki_token": fc.get("wiki_token"),
    }
    if backend == "official":
        base = OfficialFeishuBase(
            fc["app_token"],
            fc["table_id"],
            cli_path=fc.get("cli_path") or "lark-cli",
            **common,
        )
    elif backend == "legacy":
        base = FeishuBase(fc["app_token"], fc["table_id"], **common)
    else:
        print(f"未知 feishu.backend：{backend}", file=sys.stderr)
        return 2

    rename = args.rename or fc.get("rename") or "keep"
    name_field = fc.get("name_field_id")
    title_field = fc.get("title_field_id")

    try:
        records = base.list_records()
    except FeishuError as exc:
        print(f"读取飞书表格失败：{exc}", file=sys.stderr)
        if backend == "official":
            print("检查：1) lark-cli 是否可用  2) 当前飞书账号是否有表格阅读权限", file=sys.stderr)
        else:
            print("检查：1) 飞书是否已登录  2) Chrome 是否在跑", file=sys.stderr)
        return 2

    print(f"飞书表格共 {len(records)} 条记录\n")
    for rec in records:
        when = rec.submitted_at.strftime("%Y-%m-%d %H:%M") if rec.submitted_at else "?"
        name = rec.pick_name(name_field)
        title = rec.pick_title(name, title_field)
        print(
            f"  [{rec.record_id}] {when}  姓名={name or '?'}  "
            f"图名={title or '(缺)'}  附件 {len(rec.attachments)} 个"
        )

    if args.list_only:
        return 0

    report = base.sync(
        cfg["root"],
        rename=rename,
        name_field_id=name_field,
        title_field_id=title_field,
        on_date=parse_date(args.date) if args.date else None,
    )
    print(f"\n下载 {report.count} 个 -> {report.dest_dir}")
    for name, path in report.downloaded:
        print(f"  ✓ {Path(path).name}")
    for name, why in report.skipped:
        print(f"  ✗ {name}  ({why})")
    return 0 if not report.skipped else 1


def cmd_browser(args, cfg) -> int:
    """CDP Chrome 启停。"""
    from .browser import BrowserError, start, status, stop

    try:
        if args.action == "status":
            st = status(args.port)
            print(st.describe())
            return 0 if st.alive else 1
        if args.action == "start":
            print(start(args.port, headless=args.headless).describe())
            return 0
        if args.action == "stop":
            print("已关闭" if stop(args.port) else "端口本来就没监听")
            return 0
        if args.action == "restart":
            stop(args.port)
            print(start(args.port, headless=args.headless).describe())
            return 0
    except BrowserError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    return 0


def cmd_pan123_login(args, cfg) -> int:
    from .pan123 import open_login

    port = int((cfg.get("pan123") or {}).get("cdp_port") or 9222)
    print(open_login(port))
    return 0


def cmd_pan123_upload(args, cfg) -> int:
    from .pan123 import upload_folder

    pc = cfg.get("pan123") or {}
    result = upload_folder(
        args.folder,
        port=int(pc.get("cdp_port") or 9222),
        timeout=args.timeout or int(pc.get("upload_timeout") or 1800),
    )
    print(result.describe())
    return 0


def cmd_daily(args, cfg) -> int:
    """一条命令跑完当天全流程：取图 -> 归档 -> 发布。"""
    from .browser import BrowserError, ensure, status

    on_date = parse_date(args.date)
    print("=" * 60)
    print(f"逆光365 日流程    {on_date:%Y-%m-%d}")
    print("=" * 60)

    port = int((cfg.get("feishu") or {}).get("cdp_port") or 9222)
    if not status(port).alive:
        print(f"\n[0/4] CDP Chrome 未运行，尝试启动（端口 {port}）")
        try:
            print("      " + ensure(port).describe())
        except BrowserError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 2
    else:
        print(f"\n[0/4] CDP Chrome 正常（端口 {port}）")

    if not args.no_fetch:
        print("\n[1/4] 从飞书拉取投稿")
        fetch_args = argparse.Namespace(
            date=args.date, list_only=False, rename=None
        )
        code = cmd_fetch(fetch_args, cfg)
        if code != 0 and code != 1:
            print("拉取失败，中止", file=sys.stderr)
            return code
    else:
        print("\n[1/4] 跳过飞书拉取（--no-fetch）")

    print("\n[2/4] 扫描收件箱")
    scan = scan_inbox(
        cfg["root"], on_date=on_date, roster_path=cfg["roster"]
    )
    print(f"候选 {len(scan)} 张，其中入选 {len(scan.selected)} 张")
    print(_fmt_candidates(scan.candidates))

    if not args.no_archive and scan.candidates:
        print("\n[3/4] 归档")
        report = archive(
            cfg["root"], on_date=on_date, candidates=scan.candidates, copy=True
        )
        print(f"归档 {len(report.moved)} 张 -> {report.day_dir}")
    else:
        print("\n[3/4] 跳过归档")

    publish_code = 0
    if not scan.selected:
        print("\n[4/4] 没有标记「入选」的照片，不发布")
        print("      主编把选中的照片拖进 收件箱/<日期>/入选/ 或改名加（入选）")
        _report_unparsed(scan)
    else:
        print("\n[4/4] 发布")
        pub_args = argparse.Namespace(
            date=args.date,
            auto=args.auto,
            headless=not args.show_browser,
            post_time=args.post_time,
        )
        publish_code = cmd_publish(pub_args, cfg)

    if publish_code != 0:
        return publish_code
    if args.upload_pan123:
        print("\n[附加] 上传整个文件夹到 123 云盘")
        upload_args = argparse.Namespace(folder=args.upload_pan123, timeout=None)
        return cmd_pan123_upload(upload_args, cfg)
    return 0


def cmd_scan(args, cfg) -> int:
    on_date = parse_date(args.date)
    scan = scan_inbox(cfg["root"], on_date=on_date, roster_path=cfg["roster"])
    print(f"日期 {on_date:%Y-%m-%d}  收件箱候选 {len(scan)} 张\n")
    print(_fmt_candidates(scan.candidates))
    print(f"\n其中入选 {len(scan.selected)} 张")
    _report_unparsed(scan)
    return 0


def cmd_archive(args, cfg) -> int:
    on_date = parse_date(args.date)
    scan = scan_inbox(cfg["root"], on_date=on_date, roster_path=cfg["roster"])
    if not scan.candidates:
        print("没有可归档的照片")
        _report_unparsed(scan)
        return 1
    report = archive(
        cfg["root"], on_date=on_date, candidates=scan.candidates, copy=not args.move
    )
    print(f"归档目录：{report.day_dir}")
    print(f"成功 {len(report.moved)} 张，跳过 {len(report.skipped)} 张")
    for src, dst in report.moved:
        print(f"  {src}  ->  {dst}")
    for src, why in report.skipped:
        print(f"  [跳过] {src}  ({why})")
    print(f"清单：{report.manifest}")
    _report_unparsed(scan)
    return 0


def _compose_selected(cands, cfg):
    out = []
    for cand in cands:
        if not cand.selected:
            continue
        composed = build_content(
            cand.info,
            quote=cand.quote,
            xhs_handle=cand.handle,
            tags=cfg["tags"],
        )
        out.append((cand, composed))
    return out


def cmd_dry_run(args, cfg) -> int:
    on_date = parse_date(args.date)
    scan = scan_inbox(cfg["root"], on_date=on_date, roster_path=cfg["roster"])
    pairs = _compose_selected(scan.candidates, cfg)
    if not pairs:
        print("没有标记为「入选」的照片，无需发布")
        _report_unparsed(scan)
        return 1

    for cand, composed in pairs:
        print("=" * 56)
        print(f"图片：{cand.path}")
        print(f"标题：{composed.title}")
        print("-" * 56)
        print(composed.content)
        print("-" * 56)
        print(f"话题：{composed.tags}")
        for w in composed.warnings:
            print(f"⚠ {w}")
        print()
    _report_unparsed(scan)
    return 0


def cmd_publish(args, cfg) -> int:
    on_date = parse_date(args.date)
    preview = not args.auto
    scan = scan_inbox(cfg["root"], on_date=on_date, roster_path=cfg["roster"])
    pairs = _compose_selected(scan.candidates, cfg)
    if not pairs:
        print("没有标记为「入选」的照片，无需发布")
        _report_unparsed(scan)
        return 1

    mode = "预览（不点发布）" if preview else "自动发布"
    print(f"模式：{mode}    待发布 {len(pairs)} 篇\n")

    failures = 0
    for cand, composed in pairs:
        print(f"-> {composed.title}")
        for w in composed.warnings:
            print(f"   ⚠ {w}")
        result = publish_note(
            title=composed.title,
            content=composed.content,
            image=cand.path,
            preview=preview,
            headless=args.headless,
            account=cfg["account"],
            post_time=args.post_time or cfg["post_time"],
            repo=cfg["repo"],
            python=cfg["python"],
        )
        status = "成功" if result.ok else "失败"
        print(f"   {status}（returncode={result.returncode}）")
        if result.preview:
            print("   已填表，未点发布（预览模式）")
        elif result.clicked:
            print(f"   已点击发布：{result.click_detail}")
        if not result.ok:
            failures += 1
            print("   ---- 输出尾部 ----")
            print("   " + result.tail(15).replace("\n", "\n   "))
        print()

    _report_unparsed(scan)
    return 1 if failures else 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="niguang365", description="逆光365 自动化")
    p.add_argument("--root", help="覆盖配置里的项目根目录")
    sub = p.add_subparsers(dest="command", required=True)

    def add_date(sp):
        sp.add_argument("--date", help="YYYYMMDD 或 YYYY-MM-DD，默认今天")

    add_date(sub.add_parser("check-login", help="检测小红书登录态（查真实凭证）"))
    add_date(sub.add_parser("scan", help="列出当日候选照片"))
    add_date(sub.add_parser("dry-run", help="打印将发布的文案，不做任何发布"))

    ol = sub.add_parser("open-login", help="把 Chrome 导航到登录页以便扫码")
    ol.add_argument("--port", type=int, default=9222)

    pl = sub.add_parser("pan123-login", help="在项目 Chrome 中打开 123 云盘登录页")
    pu = sub.add_parser("pan123-upload", help="上传整个文件夹到 123 云盘并回读核验")
    pu.add_argument("folder", help="本地文件夹路径（不是单个文件）")
    pu.add_argument("--timeout", type=int, help="上传与核验总超时秒数，默认 1800")

    fp = sub.add_parser("fetch", help="从飞书多维表格下载收集到的图片")
    fp.add_argument("--date", help="仅当记录缺提交时间时用作兜底日期")
    fp.add_argument("--list-only", action="store_true", help="只列出，不下载")
    fp.add_argument(
        "--rename",
        choices=("keep", "convention"),
        help="convention = YYYYMMDD_姓名_图名.ext（默认读配置）",
    )

    bp = sub.add_parser("start-browser", help="启动 / 停止 CDP Chrome")
    bp.add_argument(
        "action", nargs="?", default="status", choices=("status", "start", "stop", "restart")
    )
    bp.add_argument("--port", type=int, default=9222)
    bp.add_argument("--headless", action="store_true")

    dp = sub.add_parser("daily", help="一条命令跑完当天全流程")
    dp.add_argument("--date", help="YYYYMMDD 或 YYYY-MM-DD，默认今天")
    dp.add_argument("--auto", action="store_true", help="真正点发布（默认只预览）")
    dp.add_argument("--no-fetch", action="store_true", help="跳过飞书拉取")
    dp.add_argument("--no-archive", action="store_true", help="跳过归档")
    dp.add_argument("--show-browser", action="store_true", help="发布时显示浏览器窗口")
    dp.add_argument("--post-time", help="定时发布时间，传给上游 --post-time")
    dp.add_argument(
        "--upload-pan123",
        metavar="FOLDER",
        help="发布流程结束后，把整个文件夹上传到 123 云盘并核验",
    )

    ap = sub.add_parser("archive", help="重命名并归档到已归档目录")
    add_date(ap)
    ap.add_argument("--move", action="store_true", help="移动而非复制（默认复制）")

    pp = sub.add_parser("publish", help="发布入选照片")
    add_date(pp)
    pp.add_argument("--auto", action="store_true", help="真正点发布（默认只预览）")
    pp.add_argument("--no-headless", dest="headless", action="store_false")
    pp.add_argument("--post-time", help="定时发布时间，传给上游 --post-time")
    pp.set_defaults(headless=True)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    if getattr(args, "root", None):
        cfg["root"] = args.root

    handlers = {
        "check-login": cmd_check_login,
        "open-login": cmd_open_login,
        "pan123-login": cmd_pan123_login,
        "pan123-upload": cmd_pan123_upload,
        "fetch": cmd_fetch,
        "daily": cmd_daily,
        "start-browser": cmd_browser,
        "scan": cmd_scan,
        "archive": cmd_archive,
        "dry-run": cmd_dry_run,
        "publish": cmd_publish,
    }

    try:
        return handlers[args.command](args, cfg)
    except (CollectError, ComposeError, FilenameError, PublishError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        from .browser import BrowserError
        from .feishu import FeishuError

        from .pan123 import Pan123Error

        if isinstance(exc, (BrowserError, FeishuError, Pan123Error)):
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
