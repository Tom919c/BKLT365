"""飞书平台适配器：把官方 Base 记录归一化为“投稿组”。"""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..feishu import Attachment, FeishuError, FeishuRecord, OfficialFeishuBase
from .config import FeishuConfig
from .domain import Week


DATE_TEXT = re.compile(r"^\s*(\d{4})[-/.年]?(\d{1,2})[-/.月]?(\d{1,2})日?\s*$")


@dataclass(slots=True)
class RemoteAttachment:
    token: str
    name: str
    size: int
    mime: str
    extension: str
    attachment: Attachment


@dataclass(slots=True)
class FeishuGroupData:
    record_id: str
    photographer: str
    title: str
    quote: str
    submitted_at: date
    attachments: list[RemoteAttachment] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FeishuFetchReport:
    groups: list[FeishuGroupData] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    total_records: int = 0


def _parse_text_date(value: str) -> date | None:
    match = DATE_TEXT.fullmatch(value or "")
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _field_date(rec: FeishuRecord, field_id: str) -> date | None:
    if rec.submitted_at:
        return rec.submitted_at.date()
    candidates: list[tuple[str, Any]] = []
    for name, value in rec.raw.items():
        if any(word in str(name) for word in ("日期", "时间")):
            candidates.append((str(name), value))
    text_value = rec.texts.get(field_id)
    if text_value:
        candidates.append(("explicit", text_value))
    for _name, value in candidates:
        if isinstance(value, (int, float)) and value > 1e12:
            return datetime.fromtimestamp(value / 1000).date()
        if isinstance(value, str):
            parsed = _parse_text_date(value)
            if parsed:
                return parsed
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
            except ValueError:
                continue
    return None


def _extension(att: Attachment) -> str:
    suffix = Path(att.name).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".tif", ".tiff"}:
        return suffix
    mime = (att.mime or "").lower()
    guessed = mimetypes.guess_extension(mime) if mime.startswith("image/") else None
    return (guessed or ".jpg").lower()


class FeishuAdapter:
    """只读飞书；下载到状态目录临时区，由业务层原子移动。"""

    def __init__(self, config: FeishuConfig, staging_dir: Path, *, cli_path: str = "lark-cli"):
        config.validate()
        self.config = config
        self.staging_dir = Path(staging_dir)
        self.client = OfficialFeishuBase(
            config.app_token,
            config.table_id,
            view_id=config.view_id or None,
            host=config.host,
            port=config.cdp_port,
            cli_path=cli_path,
        )

    def fetch_groups(self, week: Week) -> FeishuFetchReport:
        report = FeishuFetchReport()
        try:
            records = self.client.list_records()
        except FeishuError:
            raise
        report.total_records = len(records)
        for rec in records:
            submitted = _field_date(rec, self.config.date_field_id)
            if not submitted:
                report.skipped.append({"record_id": rec.record_id, "reason": "缺少可识别的提交日期"})
                continue
            if not week.contains(submitted):
                continue
            name = (rec.texts.get(self.config.name_field_id) or rec.pick_name(self.config.name_field_id)).strip()
            title = (rec.texts.get(self.config.title_field_id) or rec.pick_title(name, self.config.title_field_id)).strip()
            quote = (rec.texts.get(self.config.quote_field_id) or "").strip()
            if not name:
                report.skipped.append({"record_id": rec.record_id, "reason": "缺少摄影师姓名"})
                continue
            if not title:
                report.skipped.append({"record_id": rec.record_id, "reason": "缺少作品名"})
                continue
            images = [att for att in rec.attachments if att.is_image and att.token]
            if not images:
                report.skipped.append({"record_id": rec.record_id, "reason": "缺少图片附件"})
                continue
            attachments: list[RemoteAttachment] = []
            for att in images:
                size = int(att.size or 0)
                if size < 0:
                    report.skipped.append({"record_id": rec.record_id, "reason": "附件大小异常"})
                    continue
                attachments.append(
                    RemoteAttachment(
                        token=att.token,
                        name=att.name,
                        size=size,
                        mime=att.mime or "",
                        extension=_extension(att),
                        attachment=att,
                    )
                )
            if not attachments:
                report.skipped.append({"record_id": rec.record_id, "reason": "没有有效图片附件"})
                continue
            report.groups.append(
                FeishuGroupData(
                    record_id=rec.record_id,
                    photographer=name,
                    title=title,
                    quote=quote,
                    submitted_at=submitted,
                    attachments=attachments,
                )
            )
        return report

    def download(self, remote: RemoteAttachment, record_id: str) -> Path:
        """下载到按 token 隔离的临时目录，同名不同图不会相互覆盖。"""
        staging_dir = self.staging_dir / record_id / remote.token
        staging_dir.mkdir(parents=True, exist_ok=True)
        path = self.client.download(remote.attachment, staging_dir)
        if remote.size and path.stat().st_size != remote.size:
            raise FeishuError(
                f"附件大小不一致：{remote.name}，表格 {remote.size}，实际 {path.stat().st_size}"
            )
        return path
