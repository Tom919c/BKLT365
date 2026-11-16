"""123 云盘上传模块的离线测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from niguang365.cli import build_parser  # noqa: E402
from niguang365.pan123 import (  # noqa: E402
    Pan123Error,
    UploadResult,
    _human_size,
    direct_entries,
    inspect_folder,
    missing_names,
)


class TestFolderManifest(unittest.TestCase):
    def test_nested_folder_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "一周投稿"
            (root / "子目录").mkdir(parents=True)
            (root / "a.jpg").write_bytes(b"abc")
            (root / "子目录" / "b.png").write_bytes(b"12345")

            manifest = inspect_folder(root)

            self.assertEqual(manifest.file_count, 2)
            self.assertEqual(manifest.total_bytes, 8)
            self.assertEqual(
                {manifest.relative_name(p) for p in manifest.files},
                {"a.jpg", "子目录/b.png"},
            )
            self.assertEqual(direct_entries(root), ("a.jpg", "子目录"))

    def test_empty_folder_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Pan123Error):
                inspect_folder(tmp)

    def test_single_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.jpg"
            path.write_bytes(b"x")
            with self.assertRaises(Pan123Error):
                inspect_folder(path)

    def test_missing_names_preserves_expected_order(self):
        self.assertEqual(missing_names(["a", "b", "c"], {"b"}), ["a", "c"])

    def test_human_size_matches_cloud_display(self):
        self.assertEqual(_human_size(0), "0.00B")
        self.assertEqual(_human_size(1024), "1.00KB")
        self.assertEqual(_human_size(146198329), "139.43MB")


class TestUploadResult(unittest.TestCase):
    def test_existing_folder_is_not_reported_as_uploaded(self):
        result = UploadResult("一周", 2, 8, uploaded=False, remote_url="https://example")
        self.assertTrue(result.already_present)
        self.assertIn("未重复上传", result.describe())


class TestPan123Cli(unittest.TestCase):
    def test_standalone_upload_command(self):
        args = build_parser().parse_args(["pan123-upload", r"D:\photos\week1", "--timeout", "60"])
        self.assertEqual(args.command, "pan123-upload")
        self.assertEqual(args.folder, r"D:\photos\week1")
        self.assertEqual(args.timeout, 60)

    def test_daily_optional_folder_upload(self):
        args = build_parser().parse_args(["daily", "--upload-pan123", r"D:\photos\week1"])
        self.assertEqual(args.upload_pan123, r"D:\photos\week1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
