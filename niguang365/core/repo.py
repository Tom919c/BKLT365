"""SQLite 数据访问层。

返回轻量 dataclass 而不是 sqlite Row，避免业务层依赖表结构细节。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .db import Database, utc_now
from .domain import Week, parse_date


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex
    return f"{prefix}{raw}" if prefix else raw


@dataclass(slots=True)
class Photo:
    id: str
    group_id: str
    token: str
    sort_order: int
    path: Path
    ext: str
    bytes: int
    state: str
    original_name: str
    missing_remote: bool = False
    remote_name: str | None = None
    previous_remote_name: str | None = None


@dataclass(slots=True)
class Group:
    id: str
    week_id: str
    photographer: str
    title: str
    quote: str
    submitted_at: date
    status: str
    selected_date: date | None
    note_id: str | None
    conflict: str
    issue: str
    photos: list[Photo] = field(default_factory=list)


@dataclass(slots=True)
class WeekRecord:
    id: str
    start_date: date
    end_date: date
    directory: Path
    status: str
    needs_sync: bool
    closed_at: str | None = None
    uploaded_at: str | None = None
    group_count: int = 0
    photo_count: int = 0
    selected_unpublished: int = 0


def _row_to_photo(row: Any) -> Photo:
    return Photo(
        id=row["id"],
        group_id=row["group_id"],
        token=row["token"],
        sort_order=int(row["sort_order"]),
        path=Path(row["path"]),
        ext=row["ext"],
        bytes=int(row["bytes"]),
        state=row["state"],
        original_name=row["original_name"],
        missing_remote=bool(row["missing_remote"]),
        remote_name=row["remote_name"],
        previous_remote_name=row["previous_remote_name"],
    )


class Repository:
    def __init__(self, db: Database):
        self.db = db

    # ---- weeks ----------------------------------------------------
    def ensure_week(self, week: Week, directory: Path) -> WeekRecord:
        now = utc_now()
        with self.db.tx() as conn:
            existing = conn.execute("SELECT * FROM weeks WHERE id = ?", (week.id,)).fetchone()
            if not existing:
                conn.execute(
                    """
                    INSERT INTO weeks(id, start_date, end_date, directory, status, created_at, updated_at)
                    VALUES(?, ?, ?, ?, 'collecting', ?, ?)
                    """,
                    (week.id, week.start_date.isoformat(), week.end_date.isoformat(), str(directory), now, now),
                )
            elif Path(existing["directory"]) != Path(directory):
                conn.execute(
                    "UPDATE weeks SET directory = ?, updated_at = ? WHERE id = ?",
                    (str(directory), now, week.id),
                )
        return self.get_week(week.id)

    def get_week(self, week_id: str) -> WeekRecord | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM weeks WHERE id = ?", (week_id,)).fetchone()
            if not row:
                return None
            counts = conn.execute(
                """
                SELECT COUNT(DISTINCT g.id) AS groups_count,
                       COUNT(p.id) AS photos_count,
                       SUM(CASE WHEN g.status IN ('selected', 'published') AND g.note_id IS NULL THEN 1 ELSE 0 END) AS blocked
                FROM groups g LEFT JOIN photos p ON p.group_id = g.id
                WHERE g.week_id = ?
                """,
                (week_id,),
            ).fetchone()
        return WeekRecord(
            id=row["id"],
            start_date=parse_date(row["start_date"]),
            end_date=parse_date(row["end_date"]),
            directory=Path(row["directory"]),
            status=row["status"],
            needs_sync=bool(row["needs_sync"]),
            closed_at=row["closed_at"],
            uploaded_at=row["uploaded_at"],
            group_count=int(counts["groups_count"] or 0),
            photo_count=int(counts["photos_count"] or 0),
            selected_unpublished=int(counts["blocked"] or 0),
        )

    def list_weeks(self) -> list[WeekRecord]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT id FROM weeks ORDER BY start_date DESC").fetchall()
        return [record for row in rows if (record := self.get_week(row["id"]))]

    def update_week(self, week_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now()
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self.db.tx() as conn:
            conn.execute(f"UPDATE weeks SET {columns} WHERE id = ?", (*fields.values(), week_id))

    def due_weeks(self, before: date) -> list[WeekRecord]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT id FROM weeks WHERE end_date < ? ORDER BY start_date",
                (before.isoformat(),),
            ).fetchall()
        return [record for row in rows if (record := self.get_week(row["id"]))]

    # ---- groups ---------------------------------------------------
    def get_photo(self, photo_id: str) -> Photo | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        return _row_to_photo(row) if row else None

    def get_group(self, group_id: str) -> Group | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
            if not row:
                return None
            photos = conn.execute(
                "SELECT * FROM photos WHERE group_id = ? ORDER BY sort_order, created_at",
                (group_id,),
            ).fetchall()
        return Group(
            id=row["id"],
            week_id=row["week_id"],
            photographer=row["photographer"],
            title=row["title"],
            quote=row["quote"],
            submitted_at=parse_date(row["submitted_at"]),
            status=row["status"],
            selected_date=parse_date(row["selected_date"]) if row["selected_date"] else None,
            note_id=row["note_id"],
            conflict=row["conflict"],
            issue=row["issue"],
            photos=[_row_to_photo(photo) for photo in photos],
        )

    def list_groups(self, week_id: str | None = None) -> list[Group]:
        with self.db.read() as conn:
            if week_id:
                rows = conn.execute(
                    "SELECT * FROM groups WHERE week_id = ? ORDER BY submitted_at, created_at",
                    (week_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM groups ORDER BY submitted_at DESC, created_at DESC"
                ).fetchall()
            if not rows:
                return []
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            photos = conn.execute(
                f"SELECT * FROM photos WHERE group_id IN ({placeholders}) ORDER BY sort_order, created_at",
                ids,
            ).fetchall()
        photo_map: dict[str, list[Photo]] = {}
        for photo in photos:
            photo_map.setdefault(photo["group_id"], []).append(_row_to_photo(photo))
        result: list[Group] = []
        for row in rows:
            result.append(
                Group(
                    id=row["id"],
                    week_id=row["week_id"],
                    photographer=row["photographer"],
                    title=row["title"],
                    quote=row["quote"],
                    submitted_at=parse_date(row["submitted_at"]),
                    status=row["status"],
                    selected_date=parse_date(row["selected_date"]) if row["selected_date"] else None,
                    note_id=row["note_id"],
                    conflict=row["conflict"],
                    issue=row["issue"],
                    photos=photo_map.get(row["id"], []),
                )
            )
        return result

    def create_group(
        self,
        *,
        group_id: str,
        week_id: str,
        photographer: str,
        title: str,
        quote: str,
        submitted_at: date,
    ) -> Group:
        now = utc_now()
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO groups(id, week_id, photographer, title, quote, submitted_at, status,
                                   created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, 'collected', ?, ?)
                """,
                (
                    group_id,
                    week_id,
                    photographer,
                    title,
                    quote,
                    submitted_at.isoformat(),
                    now,
                    now,
                ),
            )
        group = self.get_group(group_id)
        assert group is not None
        return group

    def update_group(self, group_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now()
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self.db.tx() as conn:
            conn.execute(f"UPDATE groups SET {columns} WHERE id = ?", (*fields.values(), group_id))

    def rekey_group(self, old_id: str, new_id: str) -> None:
        """把迁移生成的临时组 ID 接到飞书 record_id 上。

        迁移工具在建组时拿不到飞书 record_id，只能生成 ``grp_...``；
        同步时识别到同一投稿后用它改键，保留照片与备注关系。
        """
        with self.db.tx() as conn:
            conn.execute("PRAGMA defer_foreign_keys = ON")
            if not conn.execute("SELECT 1 FROM groups WHERE id = ?", (old_id,)).fetchone():
                raise KeyError(old_id)
            if conn.execute("SELECT 1 FROM groups WHERE id = ?", (new_id,)).fetchone():
                raise ValueError(f"组 ID 已存在：{new_id}")
            conn.execute("UPDATE groups SET id = ?, updated_at = ? WHERE id = ?", (new_id, utc_now(), old_id))
            conn.execute("UPDATE photos SET group_id = ? WHERE group_id = ?", (new_id, old_id))
            conn.execute("UPDATE note_groups SET group_id = ? WHERE group_id = ?", (new_id, old_id))

    def apply_rename(
        self,
        *,
        group_id: str,
        plans: Sequence[Any],
        status: str | None = None,
        selected_date: date | None = None,
        note_id: str | None = None,
        remote_transition: bool = False,
    ) -> None:
        """一次事务写回整组路径与状态。plans 元素需有 photo_id/new_path。"""
        now = utc_now()
        with self.db.tx() as conn:
            for plan in plans:
                photo = conn.execute(
                    "SELECT remote_name, previous_remote_name FROM photos WHERE id = ?", (plan.photo_id,)
                ).fetchone()
                old_remote = photo["remote_name"] if photo else None
                if remote_transition and old_remote:
                    conn.execute(
                        """
                        UPDATE photos SET path = ?, previous_remote_name = ?, remote_name = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (str(plan.new_path), old_remote, now, plan.photo_id),
                    )
                else:
                    conn.execute(
                        "UPDATE photos SET path = ?, updated_at = ? WHERE id = ?",
                        (str(plan.new_path), now, plan.photo_id),
                    )
            assignments = ["updated_at = ?"]
            values: list[Any] = [now]
            if status is not None:
                assignments.append("status = ?")
                values.append(status)
            if selected_date is not None:
                assignments.append("selected_date = ?")
                values.append(selected_date.isoformat())
            if status in ("collected", "unselected") and selected_date is None:
                assignments.append("selected_date = NULL")
            if note_id is not None:
                assignments.append("note_id = ?")
                values.append(note_id)
            values.append(group_id)
            conn.execute(f"UPDATE groups SET {', '.join(assignments)} WHERE id = ?", values)

    def add_photo(self, group_id: str, token: str, path: Path, ext: str, size: int, original_name: str) -> Photo:
        photo_id = new_id("ph_")
        now = utc_now()
        with self.db.tx() as conn:
            max_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) AS value FROM photos WHERE group_id = ?", (group_id,)
            ).fetchone()["value"]
            conn.execute(
                """
                INSERT INTO photos(id, group_id, token, sort_order, path, ext, bytes, original_name,
                                   created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (photo_id, group_id, token, int(max_order) + 1, str(path), ext, int(size), original_name, now, now),
            )
        return Photo(photo_id, group_id, token, int(max_order) + 1, path, ext, size, "ready", original_name)

    def update_photo(self, photo_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now()
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self.db.tx() as conn:
            conn.execute(f"UPDATE photos SET {columns} WHERE id = ?", (*fields.values(), photo_id))

    def remove_group_photos(self, group_id: str) -> None:
        """仅在“飞书记录已删除且从未上传”时软清理，不删本地文件。"""
        with self.db.tx() as conn:
            conn.execute("UPDATE photos SET state = 'orphaned' WHERE group_id = ?", (group_id,))
            conn.execute(
                "UPDATE groups SET issue = ?, updated_at = ? WHERE id = ?",
                ("飞书原记录已删除；本地文件保留待人工处理", utc_now(), group_id),
            )

    def delete_group(self, group_id: str) -> None:
        """只用于新组落盘失败后的数据库补偿；不删除用户文件。"""
        with self.db.tx() as conn:
            conn.execute("DELETE FROM photos WHERE group_id = ?", (group_id,))
            conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))

    def delete_photos(self, photo_ids: Sequence[str]) -> None:
        if not photo_ids:
            return
        placeholders = ",".join("?" for _ in photo_ids)
        with self.db.tx() as conn:
            conn.execute(f"DELETE FROM photos WHERE id IN ({placeholders})", list(photo_ids))

    def set_photo_orders(self, orders: dict[str, int]) -> None:
        if not orders:
            return
        now = utc_now()
        with self.db.tx() as conn:
            for photo_id, order in orders.items():
                conn.execute(
                    "UPDATE photos SET sort_order = ?, updated_at = ? WHERE id = ?",
                    (int(order), now, photo_id),
                )

    def occupied_paths_except(self, group_id: str | None) -> set[Path]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT path FROM photos WHERE state != 'orphaned'").fetchall()
        paths = {Path(row["path"]) for row in rows}
        if group_id:
            with self.db.read() as conn:
                local = conn.execute("SELECT path FROM photos WHERE group_id = ?", (group_id,)).fetchall()
            paths -= {Path(row["path"]) for row in local}
        return paths

    def set_group_photos_order(self, group_id: str, photo_ids: Sequence[str]) -> None:
        with self.db.tx() as conn:
            current = conn.execute("SELECT id FROM photos WHERE group_id = ?", (group_id,)).fetchall()
            if {row["id"] for row in current} != set(photo_ids):
                raise ValueError("排序列表必须包含组内全部图片")
            for index, photo_id in enumerate(photo_ids):
                conn.execute(
                    "UPDATE photos SET sort_order = ?, updated_at = ? WHERE group_id = ? AND id = ?",
                    (index, utc_now(), group_id, photo_id),
                )

    # ---- accounts ------------------------------------------------
    def replace_accounts(self, accounts: Iterable[tuple[str, str]], source: str) -> int:
        now = utc_now()
        items = [(name.strip(), handle.strip(), source, now) for name, handle in accounts if name.strip()]
        with self.db.tx() as conn:
            conn.execute("DELETE FROM accounts")
            conn.executemany(
                "INSERT INTO accounts(name, handle, source, updated_at) VALUES(?, ?, ?, ?)", items
            )
        return len(items)

    def get_account(self, name: str) -> str | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT handle FROM accounts WHERE name = ?", (name,)).fetchone()
        return row["handle"] if row else None

    def list_accounts(self) -> list[dict[str, str]]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
        return [dict(row) for row in rows]

    # ---- notes ---------------------------------------------------
    def create_note(
        self,
        *,
        note_id: str,
        title: str,
        day_number: str,
        content: str,
        publish_date: date,
        account: str | None,
        group_ranges: list[tuple[str, int, int]],
        fingerprint: str,
        status: str = "editing",
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO notes(id, title, day_number, content, publish_date, status, account,
                                  fingerprint, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note_id,
                    title,
                    day_number,
                    content,
                    publish_date.isoformat(),
                    status,
                    account,
                    fingerprint,
                    now,
                    now,
                ),
            )
            for order, (group_id, page_start, page_end) in enumerate(group_ranges):
                conn.execute(
                    """
                    INSERT INTO note_groups(note_id, group_id, sort_order, page_start, page_end)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (note_id, group_id, order, page_start, page_end),
                )
                conn.execute(
                    "UPDATE groups SET note_id = ?, updated_at = ? WHERE id = ?",
                    (note_id, now, group_id),
                )
        note = self.get_note(note_id)
        assert note is not None
        return note

    def get_note(self, note_id: str) -> dict[str, Any] | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            if not row:
                return None
            group_rows = conn.execute(
                """
                SELECT ng.*, g.title, g.photographer, g.quote, g.status, g.week_id
                FROM note_groups ng JOIN groups g ON g.id = ng.group_id
                WHERE ng.note_id = ? ORDER BY ng.sort_order
                """,
                (note_id,),
            ).fetchall()
            topic_rows = conn.execute(
                "SELECT * FROM topics WHERE note_id = ? ORDER BY adopted DESC, sort_order, id", (note_id,)
            ).fetchall()
            draft_rows = conn.execute(
                "SELECT * FROM drafts WHERE note_id = ? ORDER BY created_at DESC", (note_id,)
            ).fetchall()
        note = dict(row)
        note["groups"] = [dict(item) for item in group_rows]
        note["topics"] = [dict(item) for item in topic_rows]
        note["drafts"] = [dict(item) for item in draft_rows]
        return note

    def list_notes(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def update_note(self, note_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now()
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self.db.tx() as conn:
            conn.execute(f"UPDATE notes SET {columns} WHERE id = ?", (*fields.values(), note_id))

    def replace_note_groups(self, note_id: str, group_ranges: list[tuple[str, int, int]]) -> None:
        now = utc_now()
        with self.db.tx() as conn:
            old = conn.execute("SELECT group_id FROM note_groups WHERE note_id = ?", (note_id,)).fetchall()
            for row in old:
                conn.execute("UPDATE groups SET note_id = NULL, updated_at = ? WHERE id = ?", (now, row["group_id"]))
            conn.execute("DELETE FROM note_groups WHERE note_id = ?", (note_id,))
            for order, (group_id, page_start, page_end) in enumerate(group_ranges):
                conn.execute(
                    "INSERT INTO note_groups(note_id, group_id, sort_order, page_start, page_end) VALUES(?, ?, ?, ?, ?)",
                    (note_id, group_id, order, page_start, page_end),
                )
                conn.execute("UPDATE groups SET note_id = ?, updated_at = ? WHERE id = ?", (note_id, now, group_id))

    def mark_note_published(self, note_id: str, status: str, note_url: str | None = None) -> None:
        now = utc_now()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE notes SET status = ?, note_url = COALESCE(?, note_url), updated_at = ? WHERE id = ?",
                (status, note_url, now, note_id),
            )
            if status == "published":
                group_rows = conn.execute("SELECT group_id FROM note_groups WHERE note_id = ?", (note_id,)).fetchall()
                for row in group_rows:
                    conn.execute(
                        "UPDATE groups SET status = 'published', updated_at = ? WHERE id = ?",
                        (now, row["group_id"]),
                    )

    # ---- drafts --------------------------------------------------
    def save_draft(self, note_id: str, locator: dict[str, Any], fingerprint: str, image_count: int, status: str) -> dict[str, Any]:
        draft_id = new_id("dr_")
        now = utc_now()
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO drafts(id, note_id, locator, fingerprint, image_count, status, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    note_id,
                    json.dumps(locator, ensure_ascii=False),
                    fingerprint,
                    image_count,
                    status,
                    now,
                    now,
                ),
            )
        return {"id": draft_id, "note_id": note_id, "locator": locator, "fingerprint": fingerprint, "image_count": image_count, "status": status}

    def update_draft(self, draft_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now()
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self.db.tx() as conn:
            conn.execute(f"UPDATE drafts SET {columns} WHERE id = ?", (*fields.values(), draft_id))

    # ---- topics --------------------------------------------------
    def replace_topics(self, note_id: str, topics: Sequence[dict[str, Any]]) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM topics WHERE note_id = ?", (note_id,))
            for index, topic in enumerate(topics):
                conn.execute(
                    """
                    INSERT INTO topics(note_id, name, views_raw, views_number, source, fetched_at, adopted, sort_order)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        note_id,
                        topic["name"],
                        topic.get("views_raw", ""),
                        topic.get("views_number"),
                        topic.get("source", ""),
                        topic.get("fetched_at"),
                        1 if topic.get("adopted") else 0,
                        topic.get("sort_order", index),
                    ),
                )

    def set_adopted_topics(self, note_id: str, names: Sequence[str]) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE topics SET adopted = 0 WHERE note_id = ?", (note_id,))
            for name in names:
                conn.execute("UPDATE topics SET adopted = 1 WHERE note_id = ? AND name = ?", (note_id, name))

    # ---- uploads -------------------------------------------------
    def create_upload_task(self, week_id: str, root_path: str) -> dict[str, Any]:
        task_id = new_id("up_")
        now = utc_now()
        with self.db.tx() as conn:
            active = conn.execute(
                "SELECT * FROM upload_tasks WHERE week_id = ? AND status IN ('queued', 'running')",
                (week_id,),
            ).fetchone()
            if active:
                return dict(active)
            conn.execute(
                """
                INSERT INTO upload_tasks(id, week_id, root_path, status, created_at)
                VALUES(?, ?, ?, 'queued', ?)
                """,
                (task_id, week_id, root_path, now),
            )
        return self.get_upload_task(task_id)

    def get_upload_task(self, task_id: str) -> dict[str, Any] | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM upload_tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return None
            items = conn.execute("SELECT * FROM upload_items WHERE task_id = ? ORDER BY filename", (task_id,)).fetchall()
        task = dict(row)
        task["summary"] = json.loads(task["summary"] or "{}")
        task["items"] = [dict(item) for item in items]
        return task

    def list_upload_tasks(self, week_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            if week_id:
                rows = conn.execute(
                    "SELECT id FROM upload_tasks WHERE week_id = ? ORDER BY created_at DESC LIMIT ?",
                    (week_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM upload_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [task for row in rows if (task := self.get_upload_task(row["id"]))]

    def latest_upload_task(self, week_id: str) -> dict[str, Any] | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT id FROM upload_tasks WHERE week_id = ? ORDER BY created_at DESC LIMIT 1",
                (week_id,),
            ).fetchone()
        return self.get_upload_task(row["id"]) if row else None

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self.db.tx() as conn:
            conn.execute(f"UPDATE upload_tasks SET {columns} WHERE id = ?", (*fields.values(), task_id))

    def replace_task_items(self, task_id: str, items: Sequence[dict[str, Any]]) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM upload_items WHERE task_id = ?", (task_id,))
            for item in items:
                conn.execute(
                    """
                    INSERT INTO upload_items(id, task_id, photo_id, filename, bytes, remote_name, status, error)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("ui_"),
                        task_id,
                        item.get("photo_id"),
                        item["filename"],
                        int(item.get("bytes", 0)),
                        item.get("remote_name"),
                        item.get("status", "pending"),
                        item.get("error", ""),
                    ),
                )

    def mark_upload_remote(self, week_id: str, filenames: Sequence[str], root_path: str) -> None:
        """上传核验成功后，把远端文件名和上一名回写到照片。"""
        now = utc_now()
        with self.db.tx() as conn:
            photos = conn.execute(
                """
                SELECT p.id, p.path, p.previous_remote_name FROM photos p
                JOIN groups g ON g.id = p.group_id WHERE g.week_id = ?
                """,
                (week_id,),
            ).fetchall()
            for photo in photos:
                name = Path(photo["path"]).name
                if name in filenames:
                    conn.execute(
                        """
                        UPDATE photos SET remote_name = ?, previous_remote_name = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (name, now, photo["id"]),
                    )
            conn.execute(
                "UPDATE weeks SET status = 'uploaded', needs_sync = 0, uploaded_at = ?, updated_at = ? WHERE id = ?",
                (now, now, week_id),
            )

    def pending_previous_remote(self, week_id: str) -> list[str]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT p.previous_remote_name FROM photos p JOIN groups g ON g.id = p.group_id
                WHERE g.week_id = ? AND p.previous_remote_name IS NOT NULL AND p.previous_remote_name != ''
                """,
                (week_id,),
            ).fetchall()
        return [row["previous_remote_name"] for row in rows]

    def clear_previous_remote(self, week_id: str) -> None:
        with self.db.tx() as conn:
            conn.execute(
                """
                UPDATE photos SET previous_remote_name = NULL, updated_at = ?
                WHERE group_id IN (SELECT id FROM groups WHERE week_id = ?)
                """,
                (utc_now(), week_id),
            )

    def week_photo_manifest(self, week_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT p.id, p.path, p.bytes, p.remote_name, p.previous_remote_name, g.title, g.status
                FROM photos p JOIN groups g ON g.id = p.group_id
                WHERE g.week_id = ? AND p.state != 'orphaned'
                ORDER BY g.submitted_at, g.created_at, p.sort_order
                """,
                (week_id,),
            ).fetchall()
        return [dict(row) for row in rows]
