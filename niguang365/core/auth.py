"""共享密码 + 服务端单会话认证。

不记录密码错误次数，不做账号锁定；同一时间只允许一个有效会话。
新登录会撤销旧会话，旧设备下一次请求收到 401 并提示重新登录。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import Database, utc_now

SESSION_COOKIE = "niguang365_session"
PASSWORD_ROUNDS = 240_000
DEFAULT_SESSION_DAYS = 7


class AuthError(RuntimeError):
    pass


def _hash_password(password: str, salt_hex: str, rounds: int = PASSWORD_ROUNDS) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), rounds
    ).hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthManager:
    def __init__(self, path: Path, db: Database):
        self.path = Path(path)
        self.db = db
        self.payload: dict[str, Any] = {}
        self.initial_password: str | None = None
        self._load_or_create()

    def _load_or_create(self) -> None:
        if self.path.exists():
            self.payload = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            password = secrets.token_urlsafe(9)
            self.payload = self.make_auth_config(password)
            self._save()
            self.initial_password = password

    @staticmethod
    def make_auth_config(password: str, session_days: int = DEFAULT_SESSION_DAYS) -> dict[str, Any]:
        salt = secrets.token_hex(16)
        return {
            "password_salt": salt,
            "password_hash": _hash_password(password, salt),
            "session_secret": secrets.token_hex(32),
            "session_days": session_days,
            "preview_headless": True,
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    @property
    def session_days(self) -> int:
        return int(self.payload.get("session_days") or DEFAULT_SESSION_DAYS)

    def verify_password(self, password: str) -> bool:
        salt = str(self.payload.get("password_salt") or "")
        expected = str(self.payload.get("password_hash") or "")
        if not salt or not expected:
            return False
        actual = _hash_password(password, salt)
        return hmac.compare_digest(actual, expected)

    def login(self, password: str, *, user_agent: str = "") -> tuple[str, str]:
        if not self.verify_password(password):
            raise AuthError("密码不正确")
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(days=self.session_days)
        session_id = secrets.token_hex(16)
        self.db.replace_session(session_id, _token_hash(token), expires.isoformat(), user_agent)
        return token, expires.isoformat()

    def authenticate(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        session = self.db.get_session(_token_hash(token))
        if not session:
            return None
        try:
            expires = datetime.fromisoformat(session["expires_at"])
        except ValueError:
            return None
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            return None
        self.db.touch_session(session["id"])
        return session

    def logout(self, token: str | None) -> None:
        if not token:
            return
        session = self.db.get_session(_token_hash(token))
        if session:
            self.db.revoke_session(session["id"])

    def change_password(self, current: str, new_password: str, *, user_agent: str = "") -> tuple[str, str]:
        if not self.verify_password(current):
            raise AuthError("当前密码不正确")
        if len(new_password.strip()) < 6:
            raise AuthError("新密码至少 6 个字符")
        if current == new_password:
            raise AuthError("新密码不能与当前密码相同")
        config = self.make_auth_config(new_password, self.session_days)
        self.payload.update(config)
        self._save()
        self.db.revoke_all_sessions()
        return self.login(new_password, user_agent=user_agent)
