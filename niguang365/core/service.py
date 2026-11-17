"""业务编排层：唯一事实源、文件物化、状态机与平台动作。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import AppConfig
from .db import Database, utc_now
from .domain import (
    FileItem,
    MergeGroup,
    NamingConflict,
    STATUS_COLLECTED,
    STATUS_PUBLISHED,
    STATUS_SELECTED,
    STATUS_UNSELECTED,
    WEEK_STATUS_CLEANUP_PENDING,
    WEEK_STATUS_CLOSED,
    WEEK_STATUS_COLLECTING,
    WEEK_STATUS_UPLOAD_BLOCKED,
    WEEK_STATUS_UPLOAD_FAILED,
    WEEK_STATUS_UPLOADED,
    Week,
    build_photo_name,
    compose_merged,
    compose_single,
    day_number,
    normalize_title,
    parse_date,
    parse_week_id,
    plan_group_rename,
    sanitize_filename_component,
    validate_day,
    validate_tags_line,
    week_for,
)
from .repo import Group, Photo, Repository, WeekRecord, new_id
from ..feishu import FeishuError


class ServiceError(RuntimeError):
    status_code = 400


class ValidationError(ServiceError):
    status_code = 422


class StateError(ServiceError):
    status_code = 409


class NotFoundError(ServiceError):
    status_code = 404


class PlatformError(ServiceError):
    status_code = 502


class UploadBlocked(StateError):
    pass


_UNSET = object()


class Service:
    def __init__(
        self,
        config: AppConfig,
        *,
        db: Database | None = None,
        feishu: Any | None = None,
        xhs: Any | None = None,
        cloud: Any | None = None,
        topics: Any | None = None,
        now: Any | None = None,
    ):
        self.config = config
        config.ensure_dirs()
        self.db = db or Database(config.db_path)
        self.db.initialize()
        self.repo = Repository(self.db)
        self.feishu = feishu
        self.xhs = xhs
        self.cloud = cloud
        self.topic_provider = topics or xhs
        self._now = now or (lambda: datetime.now().astimezone())
        self._upload_lock = threading.Lock()
        self._seed_settings()

    # ---- 基础 -----------------------------------------------------
    def now(self) -> datetime:
        return self._now()

    def _seed_settings(self) -> None:
        self.db.seed_settings(
            {
                "pan123_root": self.config.pan123_root,
                "upload_weekday": self.config.upload_weekday,
                "upload_hour": self.config.upload_hour,
                "upload_minute": self.config.upload_minute,
                "auto_start": self.config.auto_start,
                "fixed_tags": self.config.fixed_tags,
                "dynamic_topic_count": self.config.dynamic_topic_count,
                "max_topics": self.config.max_topics,
                "publish_mode": "test",
                "official_publish_authorized": False,
                "xhs_account_label": self.config.account or "",
            }
        )

    def setting(self, key: str, default: Any = None) -> Any:
        return self.db.get_setting(key, default)

    @property
    def fixed_tags(self) -> list[str]:
        return list(self.db.get_setting("fixed_tags", self.config.fixed_tags))

    @property
    def max_topics(self) -> int:
        return int(self.db.get_setting("max_topics", self.config.max_topics))

    @property
    def dynamic_topic_count(self) -> int:
        return int(self.db.get_setting("dynamic_topic_count", self.config.dynamic_topic_count))

    @property
    def pan123_root(self) -> str:
        return str(self.db.get_setting("pan123_root", self.config.pan123_root))

    def week_dir(self, week: Week) -> Path:
        return week.path(self.config.archive_parent)

    def current_week(self) -> Week:
        return week_for(self.now().date())

    def week_from_value(self, value: str | None) -> Week:
        if value:
            return parse_week_id(value)
        return self.current_week()

    def _ensure_week(self, week: Week) -> WeekRecord:
        path = self.week_dir(week)
        path.mkdir(parents=True, exist_ok=True)
        return self.repo.ensure_week(week, path)

    def _get_group(self, group_id: str) -> Group:
        group = self.repo.get_group(group_id)
        if not group:
            raise NotFoundError(f"找不到投稿组：{group_id}")
        return group

    def _get_note(self, note_id: str) -> dict[str, Any]:
        note = self.repo.get_note(note_id)
        if not note:
            raise NotFoundError(f"找不到笔记：{note_id}")
        return note

    # ---- 文件操作 -------------------------------------------------
    def _items_from_group(self, group: Group, *, replacement_paths: dict[str, Path] | None = None) -> list[FileItem]:
        replacement_paths = replacement_paths or {}
        return [
            FileItem(
                photo_id=photo.id,
                old_path=replacement_paths.get(photo.id, photo.path),
                extension=photo.ext,
                index=index,
            )
            for index, photo in enumerate(group.photos, start=1)
        ]

    def _perform_moves(self, plans: Sequence[Any]) -> list[Any]:
        moved: list[Any] = []
        for plan in plans:
            if Path(plan.old_path) == Path(plan.new_path):
                continue
            if not Path(plan.old_path).is_file():
                raise FileNotFoundError(f"源文件不存在：{plan.old_path}")
            if Path(plan.new_path).exists():
                raise NamingConflict(f"目标文件已存在，拒绝覆盖：{plan.new_path}", [Path(plan.new_path)])
            Path(plan.new_path).parent.mkdir(parents=True, exist_ok=True)
            os.replace(plan.old_path, plan.new_path)
            moved.append(plan)
        return moved

    @staticmethod
    def _rollback_moves(moved: Sequence[Any]) -> None:
        for plan in reversed(moved):
            try:
                if Path(plan.new_path).exists() and not Path(plan.old_path).exists():
                    os.replace(plan.new_path, plan.old_path)
            except OSError:
                pass

    def _plan(
        self,
        group: Group,
        *,
        title: str | None = None,
        photographer: str | None = None,
        status: str | None = None,
        selected_date: date | None | object = _UNSET,
        target_dir: Path | None = None,
        replacement_paths: dict[str, Path] | None = None,
    ) -> list[Any]:
        effective_selected = group.selected_date if selected_date is _UNSET else selected_date
        return plan_group_rename(
            title=title or group.title,
            photographer=photographer or group.photographer,
            submitted_date=group.submitted_at,
            status=status or group.status,
            selected_date=effective_selected,
            items=self._items_from_group(group, replacement_paths=replacement_paths),
            occupied_paths=self.repo.occupied_paths_except(group.id),
            target_dir=target_dir,
        )

    def _apply_group(
        self,
        group_id: str,
        plans: Sequence[Any],
        *,
        status: str | None = None,
        selected_date: date | None = None,
        remote_transition: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.repo.apply_rename(
            group_id=group_id,
            plans=plans,
            status=status,
            selected_date=selected_date,
            remote_transition=remote_transition,
        )
        if extra:
            self.repo.update_group(group_id, **extra)

    def _remote_transition_needed(self, group: Group, week: WeekRecord) -> bool:
        return week.status in {WEEK_STATUS_UPLOADED, WEEK_STATUS_CLEANUP_PENDING} or any(
            photo.remote_name for photo in group.photos
        )

    def _mark_week_dirty(self, week: WeekRecord) -> None:
        fields: dict[str, Any] = {"needs_sync": 1}
        if week.status == WEEK_STATUS_UPLOADED:
            fields["status"] = WEEK_STATUS_UPLOADED
        self.repo.update_week(week.id, **fields)

    # ---- 飞书同步 -------------------------------------------------
    def sync_week(self, week_value: str | None = None) -> dict[str, Any]:
        if not self.feishu:
            raise PlatformError("飞书适配器未配置")
        week = self.week_from_value(week_value)
        record = self._ensure_week(week)
        try:
            report = self.feishu.fetch_groups(week)
        except FeishuError as exc:
            raise PlatformError(f"飞书同步失败：{exc}") from exc
        summary = {
            "week": week.label,
            "total_records": report.total_records,
            "groups": 0,
            "new_groups": 0,
            "updated_groups": 0,
            "photos_added": 0,
            "skipped": list(report.skipped),
            "warnings": [],
        }
        seen: set[str] = set()
        for data in report.groups:
            seen.add(data.record_id)
            try:
                result = self._sync_group(week, data)
            except Exception as exc:  # noqa: BLE001
                summary["skipped"].append({"record_id": data.record_id, "reason": f"同步失败：{exc}"})
                continue
            summary["groups"] += 1
            summary["new_groups"] += 1 if result.get("created") else 0
            summary["updated_groups"] += 0 if result.get("created") else 1
            summary["photos_added"] += int(result.get("photos_added", 0))
            summary["warnings"].extend(result.get("warnings", []))
        for group in self.repo.list_groups(week.id):
            if group.id not in seen:
                self.repo.update_group(
                    group.id,
                    issue="飞书当前视图中已找不到此记录；本地文件保留待人工确认",
                )
                summary["warnings"].append(f"{group.id}：飞书记录缺失")
        if summary["new_groups"] or summary["photos_added"] or summary["updated_groups"]:
            self._mark_week_dirty(record)
        self.db.audit(
            "system",
            "sync_week",
            week.id,
            {key: summary[key] for key in ("groups", "new_groups", "photos_added")},
        )
        return summary

    def _sync_group(self, week: Week, data: Any) -> dict[str, Any]:
        record_week = week_for(data.submitted_at)
        target_week = self._ensure_week(record_week)
        existing = self.repo.get_group(data.record_id)
        if not existing:
            existing = self._adopt_legacy_group(target_week, data)
        if not existing:
            return self._sync_new_group(target_week, data)
        return self._sync_existing_group(existing, target_week, data)

    def _adopt_legacy_group(self, week: WeekRecord, data: Any) -> Group | None:
        """把迁移生成的 ``grp_...`` 组接到真实飞书 record_id。

        迁移时旧系统没有飞书记录 ID，只能生成临时组；如果不做这层识别，
        每次同步都会把同一投稿当成新组，并因为同名文件拒绝覆盖而跳过。
        仅在周、摄影师、图名、提交日完全一致且附件数量一致时配对，
        不做模糊匹配，避免把两位作者的同名作品错误合并。
        """
        candidates = [
            group
            for group in self.repo.list_groups(week.id)
            if group.photographer == data.photographer
            and group.title == data.title
            and group.submitted_at == data.submitted_at
            and any(photo.token.startswith("legacy:") for photo in group.photos)
        ]
        if len(candidates) != 1:
            return None
        group = candidates[0]
        if len(group.photos) != len(data.attachments):
            return None
        ordered = sorted(group.photos, key=lambda photo: photo.sort_order)
        try:
            self.repo.rekey_group(group.id, data.record_id)
        except (KeyError, ValueError):
            return None
        for photo, remote in zip(ordered, data.attachments):
            self.repo.update_photo(
                photo.id,
                token=remote.token,
                original_name=remote.name,
                ext=remote.extension,
                remote_name=None,
            )
        return self.repo.get_group(data.record_id)

    def _sync_new_group(self, week: WeekRecord, data: Any) -> dict[str, Any]:
        week_dir = Path(week.directory)
        week_dir.mkdir(parents=True, exist_ok=True)
        staged: dict[str, Path] = {}
        for remote in data.attachments:
            staged[remote.token] = self.feishu.download(remote, data.record_id)
        total = len(data.attachments)
        targets = []
        for index, remote in enumerate(data.attachments, start=1):
            name = build_photo_name(
                title=data.title,
                photographer=data.photographer,
                submitted_date=data.submitted_at,
                index=index,
                total=total,
                status=STATUS_COLLECTED,
                selected_date=None,
                extension=remote.extension,
            )
            target = week_dir / name
            if target.exists():
                raise NamingConflict(f"目标文件已存在，拒绝覆盖：{target.name}", [target])
            targets.append(target)
        if len({path.name for path in targets}) != len(targets):
            raise NamingConflict("组内目标文件名重复")
        group = self.repo.create_group(
            group_id=data.record_id,
            week_id=week.id,
            photographer=data.photographer,
            title=data.title,
            quote=data.quote,
            submitted_at=data.submitted_at,
        )
        photos: list[Photo] = []
        for remote, target, path in zip(data.attachments, targets, staged.values()):
            photo = self.repo.add_photo(
                group.id,
                remote.token,
                path,
                remote.extension,
                path.stat().st_size,
                remote.name,
            )
            photos.append(photo)
        group = self.repo.get_group(group.id)
        assert group is not None
        plans = [
            type("Plan", (), {"photo_id": photo.id, "old_path": staged[remote.token], "new_path": target})
            for photo, remote, target in zip(photos, data.attachments, targets)
        ]
        moved: list[Any] = []
        try:
            moved = self._perform_moves(plans)
            self._apply_group(group.id, plans, status=STATUS_COLLECTED, selected_date=None)
        except Exception:
            self._rollback_moves(moved)
            self.repo.delete_group(group.id)
            raise
        return {"created": True, "photos_added": len(photos), "warnings": []}

    def _sync_existing_group(self, group: Group, week: WeekRecord, data: Any) -> dict[str, Any]:
        week_dir = Path(week.directory)
        week_dir.mkdir(parents=True, exist_ok=True)
        by_token = {photo.token: photo for photo in group.photos}
        replacement_paths: dict[str, Path] = {}
        backups: list[tuple[Path, Path]] = []
        new_remote: list[Any] = []
        photos_added = 0
        for remote in data.attachments:
            photo = by_token.get(remote.token)
            needs_download = photo is None
            if photo is not None:
                needs_download = not photo.path.is_file() or (
                    remote.size and photo.path.is_file() and photo.path.stat().st_size != remote.size
                )
            if needs_download:
                staged = self.feishu.download(remote, data.record_id)
                replacement_paths[photo.id if photo else f"new:{remote.token}"] = staged
                if photo is None:
                    new_remote.append(remote)
                else:
                    # 损坏/缺失副本先归档，不用错误文件覆盖；失败时恢复。
                    if photo.path.is_file():
                        backup_dir = self.config.backups_root / week.id
                        backup_dir.mkdir(parents=True, exist_ok=True)
                        backup = backup_dir / f"{photo.path.stem}-{int(time.time())}{photo.path.suffix}"
                        try:
                            shutil.move(str(photo.path), str(backup))
                            backups.append((photo.path, backup))
                        except OSError:
                            pass
        created_photo_ids: list[str] = []
        for remote in new_remote:
            staged = replacement_paths[f"new:{remote.token}"]
            photo = self.repo.add_photo(
                group.id,
                remote.token,
                staged,
                remote.extension,
                staged.stat().st_size,
                remote.name,
            )
            photos_added += 1
            created_photo_ids.append(photo.id)
            replacement_paths[photo.id] = staged
        group = self.repo.get_group(group.id)
        assert group is not None
        remote_tokens = {remote.token for remote in data.attachments}
        missing_tokens = {photo.token for photo in group.photos if photo.token not in remote_tokens}
        try:
            # 在内存中先按飞书附件顺序排好，再计算文件名；数据库顺序在成功后再落盘。
            remote_order = {remote.token: index for index, remote in enumerate(data.attachments)}
            ordered_photos = sorted(
                group.photos,
                key=lambda photo: (
                    remote_order.get(photo.token, len(data.attachments) + 999),
                    photo.sort_order,
                ),
            )
            group.photos = ordered_photos
            plans = self._plan(
                group,
                title=data.title,
                photographer=data.photographer,
                target_dir=week_dir,
                replacement_paths=replacement_paths,
            )
            orders = {
                photo.id: index
                for index, photo in enumerate(ordered_photos)
            }
            if group.status == STATUS_PUBLISHED:
                # 已发布文件不允许改名，只更新元数据并留下提示。
                self.repo.update_group(
                    group.id,
                    title=data.title,
                    photographer=data.photographer,
                    quote=data.quote,
                    submitted_at=data.submitted_at.isoformat(),
                    week_id=week.id,
                    conflict="",
                    issue="已发布投稿的文件名保持不变；飞书元数据已更新",
                )
                self.repo.set_photo_orders(orders)
                return {
                    "created": False,
                    "photos_added": photos_added,
                    "warnings": ["已发布投稿的文件名保持不变"],
                }
            moved = self._perform_moves(plans)
            remote_transition = self._remote_transition_needed(group, week)
            self._apply_group(
                group.id,
                plans,
                status=group.status,
                selected_date=group.selected_date,
                remote_transition=remote_transition,
                extra={
                    "title": data.title,
                    "photographer": data.photographer,
                    "quote": data.quote,
                    "submitted_at": data.submitted_at.isoformat(),
                    "week_id": week.id,
                    "conflict": "",
                    "issue": (
                        "飞书附件已删除但本地保留："
                        + "、".join(sorted(missing_tokens))
                        if missing_tokens
                        else ""
                    ),
                },
            )
            self.repo.set_photo_orders(orders)
            for photo in group.photos:
                self.repo.update_photo(photo.id, missing_remote=1 if photo.token in missing_tokens else 0)
        except Exception:
            if created_photo_ids:
                self.repo.delete_photos(created_photo_ids)
            for original, backup in backups:
                try:
                    if backup.exists() and not original.exists():
                        original.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(backup), str(original))
                except OSError:
                    pass
            raise
        return {
            "created": False,
            "photos_added": photos_added,
            "warnings": [f"{group.id}：飞书附件数量减少，本地文件未删除"] if missing_tokens else [],
        }

    # ---- 工作台与组选择 -------------------------------------------
    def dashboard(self, week_value: str | None = None) -> dict[str, Any]:
        week = self.week_from_value(week_value)
        record = self._ensure_week(week)
        groups = self.repo.list_groups(week.id)
        payload_groups = [self._group_payload(group) for group in groups]
        return {
            "week": self._week_payload(record),
            "groups": payload_groups,
            "weeks": [self._week_payload(item) for item in self.repo.list_weeks()],
            "stats": {
                "groups": len(groups),
                "photos": sum(len(group.photos) for group in groups),
                "selected": sum(1 for group in groups if group.status in {STATUS_SELECTED, STATUS_PUBLISHED}),
                "published": sum(1 for group in groups if group.status == STATUS_PUBLISHED),
                "unselected": sum(1 for group in groups if group.status == STATUS_UNSELECTED),
            },
            "notes": self.list_notes(limit=8),
        }

    def _week_payload(self, week: WeekRecord) -> dict[str, Any]:
        return {
            "id": week.id,
            "start_date": week.start_date.isoformat(),
            "end_date": week.end_date.isoformat(),
            "directory": str(week.directory),
            "status": week.status,
            "needs_sync": week.needs_sync,
            "group_count": week.group_count,
            "photo_count": week.photo_count,
            "selected_unpublished": week.selected_unpublished,
            "closed_at": week.closed_at,
            "uploaded_at": week.uploaded_at,
        }

    def _group_payload(self, group: Group) -> dict[str, Any]:
        account = self.repo.get_account(group.photographer)
        warnings: list[str] = []
        if group.selected_date and group.selected_date < group.submitted_at:
            warnings.append(
                f"日期矛盾：投稿日 {group.submitted_at} 晚于入选日 {group.selected_date}，保留现状并请主编确认"
            )
        if not account:
            warnings.append("摄影师账号映射缺失，发布时只写姓名")
        if group.conflict:
            warnings.append(group.conflict)
        if group.issue:
            warnings.append(group.issue)
        return {
            "id": group.id,
            "week_id": group.week_id,
            "photographer": group.photographer,
            "account": account,
            "title": group.title,
            "quote": group.quote,
            "submitted_at": group.submitted_at.isoformat(),
            "status": group.status,
            "selected_date": group.selected_date.isoformat() if group.selected_date else None,
            "note_id": group.note_id,
            "conflict": group.conflict,
            "issue": group.issue,
            "warnings": warnings,
            "photos": [
                {
                    "id": photo.id,
                    "name": photo.path.name,
                    "bytes": photo.bytes,
                    "url": f"/media/photo/{photo.id}",
                    "missing_remote": photo.missing_remote,
                    "remote_name": photo.remote_name,
                }
                for photo in group.photos
            ],
        }

    def select_group(self, group_id: str, publish_date: str | date) -> dict[str, Any]:
        group = self._get_group(group_id)
        if group.status == STATUS_PUBLISHED:
            raise StateError("已发布的投稿不能取消或更改入选日期")
        if group.status == STATUS_SELECTED and group.selected_date == parse_date(publish_date):
            return self._group_payload(group)
        week = self.repo.get_week(group.week_id)
        if not week:
            raise NotFoundError(f"找不到周目录：{group.week_id}")
        plans = self._plan(
            group,
            status=STATUS_SELECTED,
            selected_date=parse_date(publish_date),
            target_dir=Path(week.directory),
        )
        moved = self._perform_moves(plans)
        try:
            self._apply_group(
                group.id,
                plans,
                status=STATUS_SELECTED,
                selected_date=parse_date(publish_date),
                remote_transition=self._remote_transition_needed(group, week),
            )
        except Exception:
            self._rollback_moves(moved)
            raise
        self._mark_week_dirty(week)
        warning = ""
        if parse_date(publish_date) < group.submitted_at:
            warning = "入选日期早于投稿日期，已保留并标记为日期矛盾，请主编确认"
        self.db.audit("editor", "select_group", group.id, {"publish_date": str(publish_date), "warning": warning})
        group = self._get_group(group.id)
        return {"group": self._group_payload(group), "warning": warning}

    def unselect_group(self, group_id: str) -> dict[str, Any]:
        group = self._get_group(group_id)
        if group.status == STATUS_PUBLISHED:
            raise StateError("已发布的投稿不能取消")
        if group.note_id:
            note = self.repo.get_note(group.note_id)
            if note and note["status"] != "published":
                raise StateError("该组已被一篇未发布笔记占用，请先删除或调整笔记")
        week = self.repo.get_week(group.week_id)
        if not week:
            raise NotFoundError("找不到周目录")
        target_status = STATUS_UNSELECTED if week.status in {WEEK_STATUS_CLOSED, WEEK_STATUS_UPLOADED, WEEK_STATUS_CLEANUP_PENDING, WEEK_STATUS_UPLOAD_FAILED, WEEK_STATUS_UPLOAD_BLOCKED} else STATUS_COLLECTED
        plans = self._plan(
            group,
            status=target_status,
            selected_date=None,
            target_dir=Path(week.directory),
        )
        moved = self._perform_moves(plans)
        try:
            self._apply_group(
                group.id,
                plans,
                status=target_status,
                selected_date=None,
                remote_transition=self._remote_transition_needed(group, week),
            )
        except Exception:
            self._rollback_moves(moved)
            raise
        if week.status in {WEEK_STATUS_UPLOADED, WEEK_STATUS_CLEANUP_PENDING}:
            self._mark_week_dirty(week)
        self.db.audit("editor", "unselect_group", group.id, {})
        return {"group": self._group_payload(self._get_group(group.id))}

    def update_group(self, group_id: str, *, title: str | None = None, quote: str | None = None) -> dict[str, Any]:
        group = self._get_group(group_id)
        new_title = title.strip() if title is not None else group.title
        if not new_title:
            raise ValidationError("作品名不能为空")
        new_quote = quote if quote is not None else group.quote
        week = self.repo.get_week(group.week_id)
        if not week:
            raise NotFoundError("找不到周目录")
        changed_name = new_title != group.title
        if changed_name and group.status == STATUS_PUBLISHED:
            raise StateError("已发布投稿不再改名，避免文件名与已发布内容不一致")
        if changed_name:
            plans = self._plan(
                group,
                title=new_title,
                status=group.status,
                selected_date=group.selected_date,
                target_dir=Path(week.directory),
            )
            moved = self._perform_moves(plans)
            try:
                self._apply_group(
                    group.id,
                    plans,
                    status=group.status,
                    selected_date=group.selected_date,
                    remote_transition=self._remote_transition_needed(group, week),
                    extra={"title": new_title, "quote": new_quote, "conflict": ""},
                )
            except Exception:
                self._rollback_moves(moved)
                raise
            self._mark_week_dirty(week)
        else:
            self.repo.update_group(group.id, quote=new_quote, conflict="")
        self.db.audit("editor", "update_group", group.id, {"title_changed": changed_name})
        return {"group": self._group_payload(self._get_group(group.id))}

    def reorder_group(self, group_id: str, photo_ids: Sequence[str]) -> dict[str, Any]:
        group = self._get_group(group_id)
        if group.status == STATUS_PUBLISHED:
            raise StateError("已发布投稿不能再调整顺序")
        self.repo.set_group_photos_order(group_id, list(photo_ids))
        group = self._get_group(group_id)
        week = self.repo.get_week(group.week_id)
        if not week:
            raise NotFoundError("找不到周目录")
        plans = self._plan(group, status=group.status, selected_date=group.selected_date, target_dir=Path(week.directory))
        moved = self._perform_moves(plans)
        try:
            self._apply_group(
                group.id,
                plans,
                status=group.status,
                selected_date=group.selected_date,
                remote_transition=self._remote_transition_needed(group, week),
            )
        except Exception:
            self._rollback_moves(moved)
            raise
        self._mark_week_dirty(week)
        return {"group": self._group_payload(self._get_group(group.id))}

    # ---- 组稿 -----------------------------------------------------
    def create_note(
        self,
        *,
        group_ids: Sequence[str],
        title: str | None,
        publish_date: str | date,
        day: str | None = None,
        dynamic_topics: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        groups = self._validate_note_groups(group_ids)
        publish_day = parse_date(publish_date)
        dates = {group.selected_date for group in groups}
        if len(dates) != 1 or publish_day not in dates:
            raise ValidationError("发布日期必须与所有入选组的入选日期一致")
        if any(group.status != STATUS_SELECTED for group in groups):
            raise StateError("只有已入选且未发布的组才能生成笔记")
        if any(group.note_id for group in groups):
            raise StateError("所选组已被其他笔记占用")
        merge = len(groups) > 1
        note_title = (title or "").strip()
        if merge and not note_title:
            raise ValidationError("合并笔记标题由主编填写")
        if not merge:
            note_title = normalize_title(note_title or groups[0].title)
        topics = list(dynamic_topics or [])
        day_value = validate_day(day or day_number(publish_day, self.config.day_baseline), publish_day)
        images = [photo for group in groups for photo in group.photos]
        if not images:
            raise ValidationError("所选组没有图片")
        if merge:
            content, ranges = compose_merged(
                note_title=note_title,
                groups=[
                    MergeGroup(
                        title=group.title,
                        photographer=group.photographer,
                        handle=self.repo.get_account(group.photographer),
                        quote=group.quote,
                        photo_count=len(group.photos),
                    )
                    for group in groups
                ],
                publish_date=publish_day,
                day=day_value,
                fixed_tags=self.fixed_tags,
                dynamic_topics=topics,
                max_topics=self.max_topics,
            )
        else:
            group = groups[0]
            content = compose_single(
                title=note_title,
                photographer=group.photographer,
                handle=self.repo.get_account(group.photographer),
                quote=group.quote,
                publish_date=publish_day,
                day=day_value,
                fixed_tags=self.fixed_tags,
                dynamic_topics=topics,
                max_topics=self.max_topics,
            )
            ranges = [(1, len(images))]
        validate_tags_line(content, self.fixed_tags, self.max_topics)
        ranges_by_group = []
        cursor = 0
        for group in groups:
            group_range = ranges[cursor] if merge else ranges[0]
            ranges_by_group.append((group.id, group_range[0], group_range[1]))
            cursor += 1
        fingerprint = note_fingerprint(
            title=note_title,
            content=content,
            images=images,
            account=self.config.account,
        )
        note_id = new_id("nt_")
        note = self.repo.create_note(
            note_id=note_id,
            title=note_title,
            day_number=day_value,
            content=content,
            publish_date=publish_day,
            account=self.config.account,
            group_ranges=ranges_by_group,
            fingerprint=fingerprint,
        )
        self.db.audit("editor", "create_note", note_id, {"groups": len(groups), "images": len(images)})
        return self.note_detail(note_id, note=note)

    def _validate_note_groups(self, group_ids: Sequence[str]) -> list[Group]:
        ids = [group_id for group_id in group_ids if group_id]
        if not ids:
            raise ValidationError("至少选择一组投稿")
        if len(set(ids)) != len(ids):
            raise ValidationError("同一组不能重复加入笔记")
        groups = [self._get_group(group_id) for group_id in ids]
        return groups

    def list_notes(self, limit: int = 50) -> list[dict[str, Any]]:
        notes = self.repo.list_notes(limit)
        # repo.list_notes 只返回 notes 表字段；页面渲染还需要 note_groups 和图片，
        # 统一走 note_detail，避免直接读取 groups 时触发 KeyError。
        return [self.note_detail(note["id"], include_images=True) for note in notes]

    def note_detail(self, note_id: str, *, note: dict[str, Any] | None = None, include_images: bool = True) -> dict[str, Any]:
        note = note or self._get_note(note_id)
        groups = []
        images = []
        for group_row in note["groups"]:
            group = self.repo.get_group(group_row["group_id"])
            if not group:
                continue
            group_payload = self._group_payload(group)
            if include_images:
                group_payload["photos"] = [
                    {
                        "id": photo.id,
                        "name": photo.path.name,
                        "bytes": photo.bytes,
                        "url": f"/media/photo/{photo.id}",
                    }
                    for photo in group.photos
                ]
                for photo in group.photos:
                    images.append({
                        "id": photo.id,
                        "url": f"/media/photo/{photo.id}",
                        "name": photo.path.name,
                        "bytes": photo.bytes,
                    })
            groups.append({"page_start": group_row["page_start"], "page_end": group_row["page_end"], **group_payload})
        note["groups"] = groups
        note["images"] = images
        note["warnings"] = self._note_warnings(note)
        return note

    def _note_warnings(self, note: dict[str, Any]) -> list[str]:
        warnings = []
        if len(note.get("title") or "") > 20:
            warnings.append("标题超过 20 字，平台可能截断；系统仅提示，不阻断")
        if len(note.get("images") or []) > 18:
            warnings.append("图片超过 18 张，小红书可能拒绝上传")
        for image in note.get("images") or []:
            if image.get("bytes", 0) > 100 * 1024 * 1024:
                warnings.append(f"{image.get('name')} 超过 100 MiB，平台可能压缩或拒绝")
        return warnings

    def update_note(
        self,
        note_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        day: str | None = None,
    ) -> dict[str, Any]:
        note = self._get_note(note_id)
        if note["status"] == "published":
            raise StateError("已发布笔记不能再编辑")
        new_title = title if title is not None else note["title"]
        new_content = content if content is not None else note["content"]
        validate_tags_line(new_content, self.fixed_tags, self.max_topics)
        new_day = validate_day(day or note["day_number"], parse_date(note["publish_date"]))
        groups = [self._get_group(row["group_id"]) for row in note["groups"]]
        images = [photo for group in groups for photo in group.photos]
        fingerprint = note_fingerprint(title=new_title, content=new_content, images=images, account=note["account"])
        self.repo.update_note(
            note_id,
            title=new_title,
            content=new_content,
            day_number=new_day,
            fingerprint=fingerprint,
        )
        return self.note_detail(note_id)

    def refresh_topics(self, note_id: str) -> dict[str, Any]:
        note = self._get_note(note_id)
        if note["status"] == "published":
            raise StateError("已发布笔记不能改话题")
        if not self.topic_provider:
            raise PlatformError("实时话题适配器未配置")
        keywords = self._topic_keywords(note)
        result = self.topic_provider.fetch_topics(keywords)
        if not result.get("ok"):
            return {
                "ok": False,
                "error": result.get("error") or "实时话题读取失败",
                "requires_manual": True,
                "topics": [],
            }
        topics = [
            topic for topic in result["topics"] if str(topic.get("name", "")).strip() not in self.fixed_tags
        ]
        existing = [topic for topic in note["topics"] if topic["name"] not in {t["name"] for t in topics}]
        # 实时结果整体替换同名的旧缓存，保留人工自定义话题。
        merged = topics + existing
        records = [
            {
                "name": topic["name"],
                "views_raw": topic.get("views_raw", ""),
                "views_number": topic.get("views_number"),
                "source": topic.get("source", ""),
                "fetched_at": topic.get("fetched_at"),
                "adopted": False,
            }
            for topic in merged
        ]
        self.repo.replace_topics(note_id, records)
        auto = [topic["name"] for topic in topics[: self.dynamic_topic_count]]
        content = self._with_topics(note["content"], self.fixed_tags + auto)
        self.repo.update_note(note_id, content=content, fingerprint=note_fingerprint(
            title=note["title"], content=content, images=self._note_images(note), account=note["account"]
        ))
        self._save_topics(note_id, auto)
        return {
            "ok": True,
            "error": result.get("error", ""),
            "topics": self._get_note(note_id)["topics"],
            "note": self.note_detail(note_id),
        }

    def save_topics(self, note_id: str, names: Sequence[str]) -> dict[str, Any]:
        note = self._get_note(note_id)
        if note["status"] == "published":
            raise StateError("已发布笔记不能改话题")
        dynamic = [name.strip() for name in names if name and name.strip() not in self.fixed_tags]
        all_topics = self.fixed_tags + dynamic
        if len(all_topics) > self.max_topics:
            raise ValidationError(f"话题总数不能超过 {self.max_topics} 个")
        content = self._with_topics(note["content"], all_topics)
        validate_tags_line(content, self.fixed_tags, self.max_topics)
        self.repo.update_note(note_id, content=content)
        self._save_topics(note_id, dynamic)
        return {"note": self.note_detail(note_id), "warning": "建议动态话题 3—4 个" if len(dynamic) > 4 else ""}

    def _save_topics(self, note_id: str, names: Sequence[str]) -> None:
        note = self._get_note(note_id)
        existing = {topic["name"]: topic for topic in note["topics"]}
        records = []
        for name in names:
            topic = existing.get(name)
            records.append(
                {
                    "name": name,
                    "views_raw": topic.get("views_raw", "") if topic else "",
                    "views_number": topic.get("views_number") if topic else None,
                    "source": topic.get("source", "manual") if topic else "manual",
                    "fetched_at": topic.get("fetched_at") if topic else None,
                    "adopted": True,
                    "sort_order": len(records),
                }
            )
        for topic in note["topics"]:
            if topic["name"] not in names:
                records.append({**topic, "adopted": False, "sort_order": len(records)})
        self.repo.replace_topics(note_id, records)

    def _with_topics(self, content: str, topics: Sequence[str]) -> str:
        lines = str(content).rstrip().splitlines()
        while lines and lines[-1].strip().startswith("#"):
            lines.pop()
        from .domain import topics_line

        lines.append(topics_line(self.fixed_tags, [topic for topic in topics if topic not in self.fixed_tags], self.max_topics))
        return "\n".join(lines)

    def _topic_keywords(self, note: dict[str, Any]) -> list[str]:
        candidates = [note["title"].strip("《》")]
        for group in note.get("groups", []):
            title = str(group.get("title") or "").strip()
            if title and title not in candidates:
                candidates.append(title)
        return candidates[:3]

    def _note_images(self, note: dict[str, Any]) -> list[Photo]:
        photos = []
        for row in note["groups"]:
            group = self.repo.get_group(row["group_id"])
            if group:
                photos.extend(group.photos)
        return photos

    # ---- 小红书 ---------------------------------------------------
    def create_draft(self, note_id: str) -> dict[str, Any]:
        if not self.xhs:
            raise PlatformError("小红书适配器未配置")
        note = self._get_note(note_id)
        if note["status"] == "published":
            raise StateError("笔记已发布")
        images = self._note_images(note)
        if not images:
            raise ValidationError("笔记没有图片")
        missing = [photo.path.name for photo in images if not photo.path.is_file()]
        if missing:
            raise StateError("图片文件缺失：" + "、".join(missing))
        try:
            result = self.xhs.create_draft(
                title=note["title"],
                content=note["content"],
                images=[photo.path for photo in images],
                expected_count=len(images),
            )
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(str(exc)) from exc
        draft = self.repo.save_draft(
            note_id,
            result["locator"],
            note["fingerprint"],
            len(images),
            "filled",
        )
        self.repo.update_note(note_id, status="draft_filled")
        self.db.audit("editor", "create_draft", note_id, {"draft_id": draft["id"], "images": len(images)})
        return {
            "draft": draft,
            "native_url": getattr(self.xhs, "native_url", lambda: "https://creator.xiaohongshu.com/publish/publish?source=official")(),
            "note": self.note_detail(note_id),
            "detail": result.get("detail", ""),
        }

    def open_draft(self, note_id: str) -> dict[str, Any]:
        if not self.xhs:
            raise PlatformError("小红书适配器未配置")
        note = self._get_note(note_id)
        drafts = note["drafts"]
        if not drafts:
            raise StateError("请先生成草稿")
        locator = json.loads(drafts[0]["locator"])
        try:
            result = self.xhs.open_draft(locator)
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(str(exc)) from exc
        return {
            "opened": bool(result.get("ok")),
            "native_url": getattr(self.xhs, "native_url", lambda: "https://creator.xiaohongshu.com/publish/publish")(),
            "state": result.get("state", {}),
        }

    def publish_note(self, note_id: str, *, confirmed: bool = False) -> dict[str, Any]:
        if not confirmed:
            raise StateError("发布前必须由主编明确确认")
        if not self.xhs:
            raise PlatformError("小红书适配器未配置")
        mode = self.setting("publish_mode", "test")
        if mode == "official" and not bool(self.setting("official_publish_authorized", False)):
            raise StateError("正式账号尚未取得本轮发布授权；请先在设置页记录授权")
        note = self._get_note(note_id)
        if note["status"] == "published":
            raise StateError("笔记已发布，不能重复发布")
        drafts = note["drafts"]
        if not drafts:
            raise StateError("请先生成草稿，再打开后台预览后发布")
        locator = json.loads(drafts[0]["locator"])
        try:
            self.xhs.open_draft(locator)
            result = self.xhs.publish_current(
                title=note["title"],
                content=note["content"],
                image_count=drafts[0]["image_count"],
            )
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"发布失败或结果不确定，请到小红书后台人工核对：{exc}") from exc
        if result.get("published"):
            self.repo.mark_note_published(note_id, "published", result.get("note_url"))
            self.repo.update_draft(drafts[0]["id"], status="published")
            self.db.audit("editor", "publish_note", note_id, {"mode": mode})
            return {"ok": True, "published": True, "message": result.get("message", "已发布"), **result}
        return {
            "ok": False,
            "published": False,
            "needs_review": True,
            "message": result.get("message") or "已点击发布但结果不确定，请人工核对",
        }

    # ---- 周收束与上传 ---------------------------------------------
    def close_week(self, week_id: str) -> dict[str, Any]:
        week = self.repo.get_week(week_id)
        if not week:
            raise NotFoundError("找不到周目录")
        groups = self.repo.list_groups(week_id)
        blocked = []
        for group in groups:
            if group.status == STATUS_SELECTED:
                if not group.note_id:
                    blocked.append(group)
                else:
                    note = self.repo.get_note(group.note_id)
                    if not note or note["status"] != "published":
                        blocked.append(group)
        if blocked:
            self.repo.update_week(week_id, status=WEEK_STATUS_UPLOAD_BLOCKED)
            self.db.audit("system", "close_week_blocked", week_id, {"groups": len(blocked)})
            return {
                "ok": False,
                "blocked": [self._group_payload(group) for group in blocked],
                "message": "仍有已入选但未发布的组，周收束已暂停；请先发布或取消入选。",
            }
        plans_by_group: dict[str, list[Any]] = {}
        all_targets: set[Path] = set()
        for group in groups:
            if group.status == STATUS_PUBLISHED:
                continue
            target_status = STATUS_UNSELECTED
            try:
                plans = self._plan(
                    group,
                    status=target_status,
                    selected_date=None,
                    target_dir=Path(week.directory),
                )
            except NamingConflict as exc:
                raise StateError(f"收束改名冲突：{exc}") from exc
            for plan in plans:
                if plan.new_path in all_targets:
                    raise StateError(f"收束目标文件名重复：{plan.new_path.name}")
                all_targets.add(plan.new_path)
            # 已经是目标命名的组不再写入数据库，避免已上传周被无意义地标记为需同步。
            unchanged = not any(
                Path(plan.new_path) != photo.path
                for plan, photo in zip(plans, group.photos)
            )
            if unchanged:
                continue
            plans_by_group[group.id] = plans
        moved_all: list[Any] = []
        try:
            for plans in plans_by_group.values():
                moved_all.extend(self._perform_moves(plans))
            for group in groups:
                if group.id not in plans_by_group:
                    continue
                self.repo.apply_rename(
                    group_id=group.id,
                    plans=plans_by_group[group.id],
                    status=STATUS_UNSELECTED,
                    selected_date=None,
                    remote_transition=self._remote_transition_needed(group, week),
                )
        except Exception:
            self._rollback_moves(moved_all)
            raise
        self.repo.update_week(
            week_id,
            status=WEEK_STATUS_CLOSED,
            closed_at=utc_now(),
            needs_sync=1 if week.status in {WEEK_STATUS_UPLOADED, WEEK_STATUS_CLEANUP_PENDING} else week.needs_sync,
        )
        self.db.audit("system", "close_week", week_id, {"groups": len(groups)})
        return {"ok": True, "week": self._week_payload(self.repo.get_week(week_id)), "blocked": []}

    def queue_upload(self, week_id: str, *, allow_current: bool = False, background: bool = True) -> dict[str, Any]:
        week = self.repo.get_week(week_id)
        if not week:
            raise NotFoundError("找不到周目录")
        if not allow_current and week.end_date >= week_for(self.now().date()).start_date:
            raise StateError("只能上传已经结束的周目录")
        closed = self.close_week(week_id)
        if not closed.get("ok"):
            raise UploadBlocked(closed.get("message") or "周收束被阻断")
        week = self.repo.get_week(week_id)
        assert week is not None
        if week.selected_unpublished:
            self.repo.update_week(week_id, status=WEEK_STATUS_UPLOAD_BLOCKED)
            raise UploadBlocked("仍有已入选但未发布的组，不能上传 123")
        task = self.repo.create_upload_task(week_id, self.pan123_root)
        if background and task["status"] == "queued":
            threading.Thread(target=self.process_upload, args=(task["id"],), daemon=True).start()
        return task

    def process_upload(self, task_id: str) -> dict[str, Any]:
        task = self.repo.get_upload_task(task_id)
        if not task:
            raise NotFoundError("找不到上传任务")
        if task["status"] not in {"queued", "failed"}:
            return task
        if not self.cloud:
            raise PlatformError("123 云盘适配器未配置")
        with self._upload_lock:
            task = self.repo.get_upload_task(task_id)
            if not task or task["status"] in {"running", "uploaded", "uploaded_with_cleanup_pending"}:
                return task  # type: ignore[return-value]
            self.repo.update_task(task_id, status="running", started_at=utc_now(), attempt=task["attempt"] + 1)
            task = self.repo.get_upload_task(task_id)
            assert task is not None
            week = self.repo.get_week(task["week_id"])
            assert week is not None
            if week.selected_unpublished:
                self.repo.update_task(
                    task_id,
                    status="failed",
                    error="仍有已入选但未发布的组，不能上传 123",
                    finished_at=utc_now(),
                )
                self.repo.update_week(week.id, status=WEEK_STATUS_UPLOAD_BLOCKED)
                return self.repo.get_upload_task(task_id) or task
            manifest = self.repo.week_photo_manifest(week.id)
            expected = [Path(item["path"]).name for item in manifest]
            pending_previous = self.repo.pending_previous_remote(week.id)
            try:
                result = self.cloud.upload_week(
                    Path(week.directory),
                    task["root_path"],
                    expected,
                )
                verified = result.get("verified", expected)
                if sorted(verified) != sorted(expected):
                    raise PlatformError("云盘回读文件清单与本地不一致")
                items = [
                    {
                        "photo_id": item["id"],
                        "filename": Path(item["path"]).name,
                        "bytes": item["bytes"],
                        "remote_name": Path(item["path"]).name,
                        "status": "verified",
                    }
                    for item in manifest
                ]
                self.repo.replace_task_items(task_id, items)
                self.repo.mark_upload_remote(week.id, expected, task["root_path"])
                self.repo.update_task(
                    task_id,
                    status="uploaded",
                    finished_at=utc_now(),
                    summary=json.dumps(
                        {
                            "file_count": result.get("file_count", len(expected)),
                            "remote_url": result.get("remote_url", ""),
                            "folder": result.get("folder", ""),
                        },
                        ensure_ascii=False,
                    ),
                )
                if pending_previous:
                    try:
                        self.cloud.cleanup_old(Path(week.directory).name, task["root_path"], pending_previous)
                        self.repo.clear_previous_remote(week.id)
                    except Exception as exc:  # noqa: BLE001
                        self.repo.update_week(
                            week.id,
                            status=WEEK_STATUS_CLEANUP_PENDING,
                            needs_sync=1,
                        )
                        self.repo.update_task(
                            task_id,
                            status="uploaded_with_cleanup_pending",
                            error=f"新文件已上传，但旧文件清理失败：{exc}",
                        )
                self.db.audit("system", "upload_week", week.id, {"task": task_id, "files": len(expected)})
                return self.repo.get_upload_task(task_id) or task
            except Exception as exc:  # noqa: BLE001
                self.repo.update_task(
                    task_id,
                    status="failed",
                    error=str(exc),
                    finished_at=utc_now(),
                )
                self.repo.update_week(week.id, status=WEEK_STATUS_UPLOAD_FAILED)
                self.db.audit("system", "upload_week_failed", week.id, {"task": task_id, "error": str(exc)[:300]})
                return self.repo.get_upload_task(task_id) or task

    def upload_tasks(self, week_id: str | None = None) -> list[dict[str, Any]]:
        return self.repo.list_upload_tasks(week_id)

    def retry_upload(self, task_id: str, *, background: bool = True) -> dict[str, Any]:
        task = self.repo.get_upload_task(task_id)
        if not task:
            raise NotFoundError("找不到上传任务")
        if task["status"] in {"queued", "running"}:
            return task
        week = self.repo.get_week(task["week_id"])
        if not week:
            raise NotFoundError("找不到周目录")
        if week.status != WEEK_STATUS_CLOSED:
            closed = self.close_week(week.id)
            if not closed.get("ok"):
                raise UploadBlocked(closed.get("message") or "周收束被阻断")
        week = self.repo.get_week(week.id)
        assert week is not None
        if week.selected_unpublished:
            raise UploadBlocked("仍有已入选但未发布的组，不能上传 123")
        self.repo.update_task(task_id, status="queued", error="", finished_at=None)
        task = self.repo.get_upload_task(task_id)
        assert task is not None
        if background:
            threading.Thread(target=self.process_upload, args=(task_id,), daemon=True).start()
        return task

    def import_accounts(self) -> dict[str, Any]:
        if not self.config.roster_path:
            raise ValidationError("配置中没有摄影师账号 CSV 路径")
        from .accounts import import_accounts as _import

        result = _import(self.db, self.config.roster_path, self.config.state_root)
        result["imported"] = result.get("count", 0)
        self.db.audit("editor", "import_accounts", str(self.config.roster_path), result)
        return result

    # ---- 设置与健康 -----------------------------------------------
    def settings_view(self) -> dict[str, Any]:
        return {
            "data_root": str(self.config.data_root),
            "archive_parent": str(self.config.archive_parent),
            "state_root": str(self.config.state_root),
            "db_path": str(self.config.db_path),
            "pan123_root": self.pan123_root,
            "upload_weekday": int(self.setting("upload_weekday", 0)),
            "upload_hour": int(self.setting("upload_hour", 20)),
            "upload_minute": int(self.setting("upload_minute", 0)),
            "auto_start": bool(self.setting("auto_start", False)),
            "fixed_tags": self.fixed_tags,
            "dynamic_topic_count": self.dynamic_topic_count,
            "max_topics": self.max_topics,
            "publish_mode": self.setting("publish_mode", "test"),
            "official_publish_authorized": bool(self.setting("official_publish_authorized", False)),
            "xhs_account_label": self.setting("xhs_account_label", ""),
            "roster_path": str(self.config.roster_path or ""),
            "scheduler_enabled": self.config.scheduler_enabled,
            "autostart_checklist": self.autostart_checklist(),
        }

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "pan123_root",
            "upload_weekday",
            "upload_hour",
            "upload_minute",
            "auto_start",
            "fixed_tags",
            "dynamic_topic_count",
            "max_topics",
            "publish_mode",
            "official_publish_authorized",
            "xhs_account_label",
        }
        updates: dict[str, Any] = {}
        for key, value in payload.items():
            if key not in allowed:
                continue
            if key == "pan123_root":
                value = str(value).strip().replace("\\", "/").strip("/")
                if not value:
                    raise ValidationError("123 年度根目录不能为空")
            elif key == "upload_weekday":
                value = int(value)
                if not 0 <= value <= 6:
                    raise ValidationError("上传星期必须在 0—6")
            elif key == "upload_hour":
                value = int(value)
                if not 0 <= value <= 23:
                    raise ValidationError("上传小时必须在 0—23")
            elif key == "upload_minute":
                value = int(value)
                if not 0 <= value <= 59:
                    raise ValidationError("上传分钟必须在 0—59")
            elif key == "fixed_tags":
                if isinstance(value, str):
                    value = [tag.strip() for tag in value.replace("，", ",").split(",") if tag.strip()]
                if not isinstance(value, list):
                    raise ValidationError("固定标签必须是字符串数组")
                value = [str(tag).strip() for tag in value if str(tag).strip()]
                if not value:
                    raise ValidationError("固定标签至少保留一个")
                value = [tag if tag.startswith("#") else f"#{tag}" for tag in value]
                if len(value) > 10:
                    raise ValidationError("固定标签不能超过 10 个")
            elif key in {"dynamic_topic_count", "max_topics"}:
                value = int(value)
                if key == "dynamic_topic_count" and not 1 <= value <= 10:
                    raise ValidationError("动态话题数必须在 1—10")
                if key == "max_topics" and not 2 <= value <= 10:
                    raise ValidationError("话题总数上限必须在 2—10")
            elif key in {"auto_start", "official_publish_authorized"}:
                value = bool(value)
            elif key == "publish_mode":
                value = str(value)
                if value not in {"test", "official"}:
                    raise ValidationError("发布模式只能是 test 或 official")
            elif key == "xhs_account_label":
                value = str(value).strip()
            updates[key] = value
        if not updates:
            raise ValidationError("没有可更新的设置")
        self.db.set_settings(updates)
        if "official_publish_authorized" in updates:
            self.db.audit(
                "editor",
                "publish_authorization",
                "xiaohongshu",
                {"granted": updates["official_publish_authorized"]},
            )
        return self.settings_view()

    def autostart_checklist(self) -> list[dict[str, str]]:
        return [
            {
                "item": "Windows 电源",
                "action": "工作室电脑设置为“永不睡眠”、合盖不睡眠；接通电源。",
                "verification": "重启后连续运行 10 分钟不进入睡眠。",
            },
            {
                "item": "Web 自启动",
                "action": "在当前用户登录自启项（HKCU Run / 启动文件夹 / 任务计划）启动 niguang365.webapp，工作目录设为仓库根目录。",
                "verification": "注销并重新登录后 http://127.0.0.1:8501 可打开。",
            },
            {
                "item": "Chrome/CDP",
                "action": "用独立 XiaohongshuProfiles\\default 启动 Chrome，固定 --remote-debugging-port=9222，加入同一自启项。",
                "verification": "http://127.0.0.1:9222/json/version 返回浏览器版本。",
            },
            {
                "item": "Cloudflare Tunnel",
                "action": "以服务方式安装 cloudflared，隧道路由 365.niguang.me → http://127.0.0.1:8501。",
                "verification": "外网手机打开 https://365.niguang.me，登录后可看到工作台。",
            },
            {
                "item": "防火墙",
                "action": "仅允许本机/局域网访问 8501；不对公网直接开放端口，公网只经 Tunnel。",
                "verification": "外网直接访问公网 IP:8501 超时。",
            },
            {
                "item": "共享密码",
                "action": "部署时设置共享密码；同一时间只允许一个主编会话。",
                "verification": "第二台设备登录后，第一台刷新即被要求重新登录。",
            },
        ]

    def health(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True, "db": str(self.config.db_path)}
        try:
            with self.db.read() as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["db_error"] = str(exc)
        if self.xhs:
            try:
                result["xhs"] = self.xhs.health()
            except Exception as exc:  # noqa: BLE001
                result["xhs"] = {"reachable": False, "logged_in": False, "detail": str(exc)}
        return result

    # ---- 账号 -----------------------------------------------------
    def accounts(self) -> list[dict[str, str]]:
        return self.repo.list_accounts()


def note_fingerprint(*, title: str, content: str, images: Iterable[Photo], account: str | None) -> str:
    payload = {
        "title": title,
        "content": content,
        "account": account,
        "images": [
            {"name": Path(photo.path).name, "bytes": photo.bytes}
            for photo in images
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
