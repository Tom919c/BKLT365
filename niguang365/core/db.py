"""SQLite 唯一事实源。

设计原则：
* 业务状态只写数据库；文件系统只是数据库状态的物化结果。
* 所有状态变更都通过事务提交，外部文件操作失败时由服务层补偿。
* WAL 模式，连接按操作短开短关，适配 FastAPI 线程池。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    name TEXT PRIMARY KEY,
    handle TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weeks (
    id TEXT PRIMARY KEY,
    start_date TEXT NOT NULL UNIQUE,
    end_date TEXT NOT NULL,
    directory TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'collecting',
    needs_sync INTEGER NOT NULL DEFAULT 0,
    closed_at TEXT,
    uploaded_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY,
    week_id TEXT NOT NULL,
    photographer TEXT NOT NULL,
    title TEXT NOT NULL,
    quote TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'collected',
    selected_date TEXT,
    note_id TEXT,
    conflict TEXT NOT NULL DEFAULT '',
    issue TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (week_id) REFERENCES weeks(id)
);

CREATE TABLE IF NOT EXISTS photos (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    path TEXT NOT NULL,
    ext TEXT NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'ready',
    original_name TEXT NOT NULL DEFAULT '',
    missing_remote INTEGER NOT NULL DEFAULT 0,
    remote_name TEXT,
    previous_remote_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (group_id) REFERENCES groups(id)
);

CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    day_number TEXT NOT NULL,
    content TEXT NOT NULL,
    publish_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'editing',
    account TEXT,
    fingerprint TEXT NOT NULL DEFAULT '',
    note_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS note_groups (
    note_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    PRIMARY KEY (note_id, group_id),
    FOREIGN KEY (note_id) REFERENCES notes(id),
    FOREIGN KEY (group_id) REFERENCES groups(id)
);

CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'xiaohongshu',
    locator TEXT NOT NULL DEFAULT '{}',
    fingerprint TEXT NOT NULL,
    image_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'filled',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (note_id) REFERENCES notes(id)
);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id TEXT NOT NULL,
    name TEXT NOT NULL,
    views_raw TEXT NOT NULL DEFAULT '',
    views_number INTEGER,
    source TEXT NOT NULL DEFAULT '',
    fetched_at TEXT,
    adopted INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (note_id, name),
    FOREIGN KEY (note_id) REFERENCES notes(id)
);

CREATE TABLE IF NOT EXISTS upload_tasks (
    id TEXT PRIMARY KEY,
    week_id TEXT NOT NULL,
    root_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '{}',
    attempt INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (week_id) REFERENCES weeks(id)
);

CREATE TABLE IF NOT EXISTS upload_items (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    photo_id TEXT,
    filename TEXT NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0,
    remote_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (task_id) REFERENCES upload_tasks(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_activity TEXT NOT NULL,
    revoked_at TEXT,
    user_agent TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id TEXT PRIMARY KEY,
    week_id TEXT,
    status TEXT NOT NULL,
    report TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_groups_week ON groups(week_id, status);
CREATE INDEX IF NOT EXISTS idx_photos_group ON photos(group_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_notes_date ON notes(publish_date, status);
CREATE INDEX IF NOT EXISTS idx_upload_week ON upload_tasks(week_id, status);
CREATE INDEX IF NOT EXISTS idx_topics_note ON topics(note_id, adopted);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def initialize(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ---- settings -------------------------------------------------
    def seed_settings(self, values: dict[str, Any]) -> None:
        with self.tx() as conn:
            for key, value in values.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES(?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), utc_now()),
                )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.read() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def get_settings(self) -> dict[str, Any]:
        with self.read() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                result[row["key"]] = row["value"]
        return result

    def set_setting(self, key: str, value: Any) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES(?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), utc_now()),
            )

    def set_settings(self, values: dict[str, Any]) -> None:
        with self.tx() as conn:
            now = utc_now()
            for key, value in values.items():
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES(?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )

    # ---- audit ----------------------------------------------------
    def audit(self, actor: str, action: str, target: str = "", detail: dict[str, Any] | None = None) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO audit_logs(at, actor, action, target, detail) VALUES(?, ?, ?, ?, ?)",
                (utc_now(), actor, action, target, json.dumps(detail or {}, ensure_ascii=False)),
            )

    def recent_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- sessions -------------------------------------------------
    def replace_session(self, session_id: str, token_hash: str, expires_at: str, user_agent: str) -> None:
        """新登录撤销全部旧会话，保证同一时间只有一个有效主编。"""
        now = utc_now()
        with self.tx() as conn:
            conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE revoked_at IS NULL", (now,)
            )
            conn.execute(
                """
                INSERT INTO sessions(id, token_hash, created_at, expires_at, last_activity, user_agent)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (session_id, token_hash, now, expires_at, now, user_agent[:500]),
            )

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE token_hash = ? AND revoked_at IS NULL", (token_hash,)
            ).fetchone()
        return dict(row) if row else None

    def touch_session(self, session_id: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE sessions SET last_activity = ? WHERE id = ?", (utc_now(), session_id)
            )

    def revoke_session(self, session_id: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE id = ?", (utc_now(), session_id)
            )

    def revoke_all_sessions(self) -> None:
        with self.tx() as conn:
            conn.execute("UPDATE sessions SET revoked_at = ? WHERE revoked_at IS NULL", (utc_now(),))


def parse_json_field(row: dict[str, Any], key: str, default: Any) -> Any:
    value = row.get(key)
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
