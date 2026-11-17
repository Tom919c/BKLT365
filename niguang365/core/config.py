"""运行配置与默认业务口径。

不要把真实凭据、Cookie 或本机绝对路径提交到仓库。`config.json` 与
`web.local.json` 均在 `.gitignore` 中。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_ROOT.parent


DEFAULT_FIXED_TAGS = ["#电子科技大学", "#电子科技大学逆光摄影"]
DEFAULT_DAY_BASELINE = "2022-11-15"


def _as_path(value: str | os.PathLike[str] | None) -> Path | None:
    if not value:
        return None
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def _first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return default


@dataclass(slots=True)
class FeishuConfig:
    host: str
    app_token: str
    table_id: str
    view_id: str = ""
    wiki_token: str = ""
    form_share_token: str = ""
    cdp_port: int = 9222
    name_field_id: str = ""
    title_field_id: str = ""
    date_field_id: str = ""
    image_field_id: str = ""
    quote_field_id: str = ""
    attachment_dir: str = ""
    cli_path: str = "lark-cli"

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "FeishuConfig":
        notes = payload.get("field_notes") or {}
        image_field_id = payload.get("image_field_id", "")
        if not image_field_id:
            for fid, note in notes.items():
                if "图片" in str(note) or "附件" in str(note):
                    image_field_id = fid
                    break
        date_field_id = payload.get("date_field_id", "")
        if not date_field_id:
            for fid, note in notes.items():
                if "时间" in str(note) or "日期" in str(note):
                    date_field_id = fid
                    break
        quote_field_id = payload.get("quote_field_id", "fldI8aQytg")
        return cls(
            host=str(payload.get("host", "")).rstrip("/"),
            app_token=str(payload.get("app_token", "")),
            table_id=str(payload.get("table_id", "")),
            view_id=str(payload.get("view_id", "")),
            wiki_token=str(payload.get("wiki_token", "")),
            form_share_token=str(payload.get("form_share_token", "")),
            cdp_port=int(payload.get("cdp_port", 9222)),
            name_field_id=str(payload.get("name_field_id", "fldzKqqKKW")),
            title_field_id=str(payload.get("title_field_id", "fldWYlP7uA")),
            date_field_id=str(date_field_id or "fldClZDe0v"),
            image_field_id=str(image_field_id or "fldf8LqT04"),
            quote_field_id=str(quote_field_id),
            attachment_dir=str(payload.get("attachment_dir", "")),
            cli_path=str(payload.get("cli_path") or "lark-cli"),
        )

    def validate(self) -> None:
        missing = [
            name
            for name, value in {
                "feishu.host": self.host,
                "feishu.app_token": self.app_token,
                "feishu.table_id": self.table_id,
                "feishu.name_field_id": self.name_field_id,
                "feishu.title_field_id": self.title_field_id,
                "feishu.date_field_id": self.date_field_id,
                "feishu.image_field_id": self.image_field_id,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError("飞书配置缺少：" + "、".join(missing))


@dataclass(slots=True)
class AppConfig:
    data_root: Path
    archive_parent: Path
    state_root: Path
    db_path: Path
    roster_path: Path | None = None
    repo_path: Path | None = None
    python_path: str = "python"
    account: str | None = None
    fixed_tags: list[str] = field(default_factory=lambda: list(DEFAULT_FIXED_TAGS))
    dynamic_topic_count: int = 3
    max_topics: int = 10
    day_baseline: str = DEFAULT_DAY_BASELINE
    cdp_port: int = 9222
    pan123_root: str = "我的文件/逆光摄影工作室/逆光日记365/2026-2027"
    pan123_timeout: int = 1800
    upload_weekday: int = 0
    upload_hour: int = 20
    upload_minute: int = 0
    auto_start: bool = False
    scheduler_enabled: bool = True
    feishu: FeishuConfig | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    web: dict[str, Any] = field(default_factory=dict)

    @property
    def staging_root(self) -> Path:
        return self.state_root / "staging"

    @property
    def backups_root(self) -> Path:
        return self.state_root / "backups"

    @property
    def manifests_root(self) -> Path:
        return self.state_root / "upload-manifests"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_root,
            self.archive_parent,
            self.state_root,
            self.staging_root,
            self.backups_root,
            self.manifests_root,
            self.db_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"配置文件必须是 JSON 对象：{path}")
    return payload


def load_config(config_path: str | Path | None = None, *, require_feishu: bool = False) -> AppConfig:
    """读取本机配置并补齐默认值。"""
    path = Path(config_path) if config_path else APP_ROOT / "config.json"
    payload = _load_json(path)

    root_value = _first(payload, "data_root", "root", default=str(Path.home() / "逆光365图片下载"))
    data_root = _as_path(root_value)
    assert data_root is not None
    archive_value = _first(payload, "archive_parent", default=str(data_root / "已归档"))
    archive_parent = _as_path(archive_value)
    assert archive_parent is not None
    state_value = _first(payload, "state_root", default=str(data_root / ".niguang365"))
    state_root = _as_path(state_value)
    assert state_root is not None
    db_value = _first(payload, "db_path", default=str(state_root / "state" / "niguang365.sqlite3"))
    db_path = _as_path(db_value)
    assert db_path is not None

    tags = payload.get("fixed_tags", payload.get("tags") or DEFAULT_FIXED_TAGS)
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("fixed_tags 必须是字符串数组")
    # 旧配置里的 #逆光日记 不再属于固定标签，静默忽略，避免把旧口径带进新链路。
    fixed_tags = [tag.strip() for tag in tags if tag.strip() and tag.strip() != "#逆光日记"]
    if not fixed_tags:
        fixed_tags = list(DEFAULT_FIXED_TAGS)

    upload = payload.get("upload") or {}
    pan123 = payload.get("pan123") or {}
    xhs = payload.get("xhs") or {}
    feishu_payload = payload.get("feishu") or {}
    feishu = FeishuConfig.from_mapping(feishu_payload) if feishu_payload else None
    if require_feishu and feishu:
        feishu.validate()

    web = _load_json(APP_ROOT / "web.local.json")
    if payload.get("web"):
        web.update(payload["web"])
    # 兼容旧顶层字段。
    for key in ("password_salt", "password_hash", "session_secret", "session_days", "preview_headless"):
        if key in payload and key not in web:
            web[key] = payload[key]

    return AppConfig(
        data_root=data_root,
        archive_parent=archive_parent,
        state_root=state_root,
        db_path=db_path,
        roster_path=_as_path(payload.get("roster")),
        repo_path=_as_path(payload.get("repo")),
        python_path=str(payload.get("python") or "python"),
        account=payload.get("account"),
        fixed_tags=fixed_tags,
        dynamic_topic_count=int(payload.get("dynamic_topic_count", 3)),
        max_topics=int(payload.get("max_topics", 10)),
        day_baseline=str(payload.get("day_baseline", DEFAULT_DAY_BASELINE)),
        cdp_port=int(payload.get("cdp_port", feishu.cdp_port if feishu else 9222)),
        pan123_root=str(
            upload.get(
                "root_path",
                pan123.get("root_path", "我的文件/逆光摄影工作室/逆光日记365/2026-2027"),
            )
        ),
        pan123_timeout=int(pan123.get("upload_timeout", 1800)),
        upload_weekday=int(upload.get("weekday", 0)),
        upload_hour=int(upload.get("hour", 20)),
        upload_minute=int(upload.get("minute", 0)),
        auto_start=bool(upload.get("auto_start", False)),
        scheduler_enabled=bool(payload.get("scheduler_enabled", True)),
        feishu=feishu,
        raw=payload,
        web=web,
    )
