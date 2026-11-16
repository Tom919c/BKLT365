"""摄影师姓名 → 小红书号映射。"""

from __future__ import annotations

import csv
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .db import Database


NAME_HEADERS = ("姓名", "摄影师", "name", "Name")
HANDLE_HEADERS = ("小红书号", "小红书账号", "账号", "handle", "xiaohongshu", "小红书")


class AccountCsvError(RuntimeError):
    pass


def _detect_encoding(path: Path) -> str:
    raw = path.read_bytes()[:4096]
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        with path.open("r", encoding="utf-8", errors="strict") as handle:
            handle.read(1024)
        return "utf-8"
    except UnicodeDecodeError:
        return "gb18030"


def read_accounts(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        raise AccountCsvError(f"摄影师账号表不存在：{path}")
    encoding = _detect_encoding(path)
    with path.open("r", encoding=encoding, newline="") as handle:
        sample = handle.read(2048)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        if not reader.fieldnames:
            raise AccountCsvError("账号表没有表头")
        headers = {str(name).strip(): name for name in reader.fieldnames}
        name_key = next((headers[key] for key in NAME_HEADERS if key in headers), None)
        handle_key = next((headers[key] for key in HANDLE_HEADERS if key in headers), None)
        if not name_key or not handle_key:
            raise AccountCsvError(
                "账号表需要包含“姓名”和“小红书号”两列，当前表头："
                + "、".join(str(name) for name in reader.fieldnames)
            )
        rows: list[tuple[str, str]] = []
        for row in reader:
            name = str(row.get(name_key, "")).strip()
            handle = str(row.get(handle_key, "")).strip()
            if not name or not handle:
                continue
            rows.append((name, handle))
    return rows


def import_accounts(db: Database, path: Path, state_root: Path) -> dict[str, object]:
    accounts = read_accounts(path)
    if not accounts:
        raise AccountCsvError("账号表没有有效行")
    backup_dir = state_root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"photographer-accounts-{stamp}.csv"
    shutil.copy2(path, backup)
    count = db.replace_accounts(accounts, source=str(path))
    return {"count": count, "backup": str(backup)}
