"""123 云盘适配器：年度根目录上传、核验与受控旧文件清理。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..pan123 import Pan123Error, delete_remote_files, upload_folder_to_root


class CloudAdapterError(RuntimeError):
    pass


class Pan123Adapter:
    def __init__(self, port: int = 9222, timeout: int = 1800):
        self.port = port
        self.timeout = timeout

    def upload_week(
        self,
        folder: Path,
        root_path: str,
        expected_files: list[str],
        *,
        log: Callable[[str], None] = print,
    ) -> dict[str, Any]:
        try:
            result = upload_folder_to_root(
                folder,
                root_path,
                port=self.port,
                timeout=self.timeout,
                log=log,
            )
        except Pan123Error as exc:
            raise CloudAdapterError(str(exc)) from exc
        if result.file_count != len(expected_files):
            raise CloudAdapterError(
                f"云盘核验文件数为 {result.file_count}，本地清单为 {len(expected_files)}"
            )
        return {
            "folder": result.folder,
            "file_count": result.file_count,
            "total_bytes": result.total_bytes,
            "uploaded": result.uploaded,
            "remote_url": result.remote_url,
            "verified": list(expected_files),
        }

    def cleanup_old(self, folder_name: str, root_path: str, names: list[str]) -> list[str]:
        try:
            return delete_remote_files(
                folder_name,
                root_path,
                names,
                port=self.port,
                timeout=self.timeout,
            )
        except Pan123Error as exc:
            raise CloudAdapterError(str(exc)) from exc
