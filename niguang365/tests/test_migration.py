"""旧收件箱迁移的代码级测试；不访问飞书、小红书或 123。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from niguang365.core.config import AppConfig
from niguang365.core.db import Database
from niguang365.core.repo import Repository
from niguang365.migrate_legacy import migrate


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / "收件箱" / "20261109"
        self.inbox.mkdir(parents=True)
        (self.inbox / "20261109_邬俊_作品.jpg").write_bytes(b"first")
        (self.inbox / "20261109_邬俊_作品02.jpg").write_bytes(b"second")
        (self.inbox / ".niguang365-selection.json").write_text(
            json.dumps(
                {
                    "selected": [
                        "20261109_邬俊_作品.jpg",
                        "20261109_邬俊_作品02.jpg",
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.config = AppConfig(
            data_root=self.root,
            archive_parent=self.root / "已归档",
            state_root=self.root / ".state",
            db_path=self.root / ".state" / "db.sqlite3",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_does_not_write(self):
        report = migrate(self.config, apply=False)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["group_count"], 1)
        self.assertEqual(report["selected_groups"], 1)
        self.assertEqual(report["groups"][0]["photos"], 2)
        self.assertFalse(self.config.db_path.exists())
        self.assertFalse((self.root / "已归档").exists())

    def test_apply_copies_week_directory_and_writes_database(self):
        report = migrate(self.config, apply=True)
        self.assertEqual(report["summary"]["groups"], 1)
        week_dir = self.root / "已归档" / "2026" / "20261109-1115"
        self.assertTrue(
            (week_dir / "20261109_邬俊_作品1（1109入选）.jpg").exists()
        )
        self.assertTrue(
            (week_dir / "20261109_邬俊_作品2（1109入选）.jpg").exists()
        )
        db = Database(self.config.db_path)
        db.initialize()
        group = Repository(db).list_groups()[0]
        self.assertEqual(group.status, "selected")
        self.assertEqual(len(group.photos), 2)
        self.assertTrue((self.root / "收件箱" / "20261109" / "20261109_邬俊_作品.jpg").exists())


if __name__ == "__main__":
    unittest.main()
