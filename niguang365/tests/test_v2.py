"""v2 业务规则、编排、认证与 Web API 的逻辑测试。

不接触真实飞书、小红书、123 云盘，也不修改用户目录。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from niguang365.core.auth import AuthError, AuthManager
from niguang365.core.config import AppConfig, DEFAULT_FIXED_TAGS, FeishuConfig
from niguang365.core.domain import (
    FileItem,
    MergeGroup,
    NamingConflict,
    STATUS_SELECTED,
    STATUS_UNSELECTED,
    STATUS_COLLECTED,
    build_photo_name,
    compose_merged,
    compose_single,
    day_number,
    plan_group_rename,
    validate_tags_line,
    week_for,
)
from niguang365.core.scheduler import UploadScheduler
from niguang365.core.service import PlatformError, Service, StateError
from niguang365.feishu import FeishuError
from niguang365.webapp import create_app


class FakeRemote:
    def __init__(self, token: str, source: Path, extension: str, name: str | None = None):
        self.token = token
        self.source = source
        self.name = name or source.name
        self.size = source.stat().st_size
        self.mime = "image/jpeg"
        self.extension = extension
        self.attachment = self


@dataclass
class FakeGroupData:
    record_id: str
    photographer: str
    title: str
    quote: str
    submitted_at: date
    attachments: list[FakeRemote] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


@dataclass
class FakeReport:
    groups: list[FakeGroupData]
    skipped: list[dict] = field(default_factory=list)
    total_records: int = 0


class FakeFeishu:
    def __init__(self, groups: list[FakeGroupData], staging: Path):
        self.groups = groups
        self.staging = staging

    def fetch_groups(self, week):
        return FakeReport(groups=self.groups, total_records=len(self.groups))

    def download(self, remote: FakeRemote, record_id: str) -> Path:
        target_dir = self.staging / record_id / remote.token
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / remote.name
        shutil.copyfile(remote.source, target)
        return target


class FakeXhs:
    def __init__(self):
        self.drafts = []
        self.published = 0

    def native_url(self):
        return "https://creator.xiaohongshu.com/publish/publish?source=official"

    def fetch_topics(self, keywords, *, max_candidates=30):
        return {
            "ok": True,
            "topics": [
                {"name": "#校园摄影", "views_raw": "2.1万", "views_number": 21000, "source": "实时搜索：校园", "fetched_at": "2026-11-17T09:00:00+08:00"},
                {"name": "#成都", "views_raw": "1.2万", "views_number": 12000, "source": "实时搜索：成都", "fetched_at": "2026-11-17T09:00:00+08:00"},
                {"name": "#逆光", "views_raw": "9000", "views_number": 9000, "source": "实时搜索：逆光", "fetched_at": "2026-11-17T09:00:00+08:00"},
                {"name": "#电子科技大学", "views_raw": "8888", "views_number": 8888, "source": "实时搜索：电子科技大学", "fetched_at": "2026-11-17T09:00:00+08:00"},
            ],
            "error": "",
        }

    def create_draft(self, *, title, content, images, expected_count):
        assert len(images) == expected_count
        assert all(Path(item).is_file() for item in images)
        self.drafts.append({"title": title, "content": content, "images": list(images)})
        return {
            "locator": {"title": title, "content_sha": "fake", "image_count": len(images), "created_at": 1},
            "image_count": len(images),
            "page_state": {"url": "https://creator.xiaohongshu.com/publish/publish"},
            "detail": "fake",
        }

    def open_draft(self, locator):
        return {"ok": True, "state": {"title": locator.get("title"), "images": locator.get("image_count")}, "url": "https://creator.xiaohongshu.com/publish/publish"}

    def publish_current(self, *, title, content, image_count):
        self.published += 1
        return {"published": True, "message": "已发布", "note_url": "https://www.xiaohongshu.com/explore/fake"}


class FakeCloud:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.uploads = []
        self.deleted = []

    def upload_week(self, folder, root_path, expected_files):
        if self.fail:
            raise RuntimeError("模拟上传失败")
        names = [Path(name).name for name in expected_files]
        self.uploads.append((Path(folder), root_path, names))
        return {
            "folder": Path(folder).name,
            "file_count": len(names),
            "total_bytes": sum((Path(folder) / name).stat().st_size for name in names),
            "uploaded": True,
            "remote_url": f"https://yun.123pan.cn/{root_path}/{Path(folder).name}",
            "verified": names,
        }

    def cleanup_old(self, folder_name, root_path, names):
        self.deleted.append((folder_name, root_path, list(names)))
        return list(names)


class Fixture:
    def __init__(self, base: Path, *, cloud_fail: bool = False):
        self.base = base
        self.data = base / "data"
        self.config = AppConfig(
            data_root=self.data,
            archive_parent=self.data / "已归档",
            state_root=self.data / ".niguang365",
            db_path=self.data / ".niguang365" / "state" / "state.sqlite3",
            fixed_tags=list(DEFAULT_FIXED_TAGS),
            scheduler_enabled=False,
        )
        self.sources = base / "sources"
        self.sources.mkdir(parents=True, exist_ok=True)
        self.groups = self._make_groups()
        self.feishu = FakeFeishu(self.groups, self.config.staging_root)
        self.xhs = FakeXhs()
        self.cloud = FakeCloud(fail=cloud_fail)
        self.service = Service(
            self.config,
            feishu=self.feishu,
            xhs=self.xhs,
            cloud=self.cloud,
            topics=self.xhs,
            now=lambda: datetime(2026, 11, 17, 9, 0, tzinfo=timezone(timedelta(hours=8))),
        )

    def _image(self, name: str, payload: bytes | None = None) -> Path:
        path = self.sources / name
        path.write_bytes(payload or (b"fake-image-" + name.encode("utf-8")))
        return path

    def _make_groups(self) -> list[FakeGroupData]:
        a1 = FakeRemote("tok-a1", self._image("a1.jpg"), ".jpg")
        a2 = FakeRemote("tok-a2", self._image("a2.jpg"), ".jpg")
        b1 = FakeRemote("tok-b1", self._image("b1.png"), ".png")
        c1 = FakeRemote("tok-c1", self._image("c1.jpg"), ".jpg")
        d = [FakeRemote(f"tok-d{i}", self._image(f"d{i}.jpg"), ".jpg") for i in range(1, 5)]
        return [
            FakeGroupData(
                record_id="rec-a",
                photographer="邝俊",
                title="你看你的我看我的",
                quote="第一行图说\n第二行图说 😀",
                submitted_at=date(2026, 11, 9),
                attachments=[a1, a2],
            ),
            FakeGroupData(
                record_id="rec-b",
                photographer="陈权",
                title="美的感受",
                quote="",
                submitted_at=date(2026, 11, 10),
                attachments=[b1],
            ),
            FakeGroupData(
                record_id="rec-c",
                photographer="李乾澄",
                title="拼接的世界",
                quote="拼接",
                submitted_at=date(2026, 11, 11),
                attachments=[c1],
            ),
            FakeGroupData(
                record_id="rec-d",
                photographer="黄鑫",
                title="原来飞机路过黄昏，也会沾上一身晚霞",
                quote="",
                submitted_at=date(2026, 11, 15),
                attachments=d,
            ),
        ]


class DomainTests(unittest.TestCase):
    def test_week_and_day_number(self):
        week = week_for(date(2026, 11, 13))
        self.assertEqual(week.start_date, date(2026, 11, 9))
        self.assertEqual(week.directory_name, "20261109-1115")
        self.assertEqual(day_number(date(2026, 11, 13)), "day1460")
        self.assertEqual(day_number(date(2026, 11, 10)), "day1457")

    def test_build_photo_name_multi_and_suffix(self):
        name = build_photo_name(
            title="党岭",
            photographer="施彦烺",
            submitted_date=date(2026, 11, 12),
            index=1,
            total=6,
            status=STATUS_SELECTED,
            selected_date=date(2026, 11, 12),
            extension=".jpg",
        )
        self.assertEqual(name, "20261112_施彦烺_党岭1（1112入选）.jpg")
        self.assertEqual(
            build_photo_name(
                title="党岭",
                photographer="施彦烺",
                submitted_date=date(2026, 11, 12),
                index=1,
                total=1,
                status=STATUS_UNSELECTED,
                selected_date=None,
                extension=".jpg",
            ),
            "20261112_施彦烺_党岭（未入选）.jpg",
        )

    def test_compose_single_and_merged(self):
        content = compose_single(
            title="冷暖乐章",
            photographer="陈冬青",
            handle="chen",
            quote="图说第一行\n图说第二行",
            publish_date=date(2026, 11, 13),
            day=None,
            fixed_tags=DEFAULT_FIXED_TAGS,
            dynamic_topics=["校园摄影"],
        )
        lines = content.splitlines()
        self.assertEqual(lines[0], "day1460")
        self.assertEqual(lines[1], "《冷暖乐章》")
        self.assertIn("摄影师：陈冬青@chen", lines)
        self.assertIn("图说第二行", lines)
        self.assertEqual(lines[-1], "#电子科技大学 #电子科技大学逆光摄影 #校园摄影")
        ranges = []
        merged, ranges = compose_merged(
            note_title="同一主题",
            groups=[
                MergeGroup("作品一", "甲", "", "说明一", 3),
                MergeGroup("作品二", "乙", "yi", "", 2),
            ],
            publish_date=date(2026, 11, 13),
            day=None,
            fixed_tags=DEFAULT_FIXED_TAGS,
            dynamic_topics=[],
        )
        self.assertEqual(ranges, [(1, 3), (4, 5)])
        self.assertIn("p1-p3:《作品一》", merged)
        self.assertIn("p4-p5:《作品二》", merged)

    def test_tags_validation(self):
        self.assertEqual(
            validate_tags_line("正文\n#电子科技大学 #电子科技大学逆光摄影 #校园"),
            ["#电子科技大学", "#电子科技大学逆光摄影", "#校园"],
        )
        with self.assertRaises(ValueError):
            validate_tags_line("正文\n#电子科技大学")
        with self.assertRaises(ValueError):
            validate_tags_line("正文\n#电子科技大学 #电子科技大学逆光摄影\n多了一行")

    def test_plan_group_rename_rejects_existing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.jpg"
            old.write_bytes(b"a")
            occupied = root / "20261112_施彦烺_党岭.jpg"
            occupied.write_bytes(b"b")
            items = [FileItem("p1", old, ".jpg", 1)]
            with self.assertRaises(NamingConflict):
                plan_group_rename(
                    title="党岭",
                    photographer="施彦烺",
                    submitted_date=date(2026, 11, 12),
                    status="collected",
                    selected_date=None,
                    items=items,
                    target_dir=root,
                )


class FeishuConfigTests(unittest.TestCase):
    def test_cli_path_is_preserved(self):
        config = FeishuConfig.from_mapping(
            {
                "host": "https://example.feishu.cn",
                "app_token": "app",
                "table_id": "table",
                "cli_path": r"D:\逆光365图片下载\.niguang365\tools\lark-cli.exe",
            }
        )
        self.assertEqual(
            config.cli_path,
            r"D:\逆光365图片下载\.niguang365\tools\lark-cli.exe",
        )


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = Fixture(Path(self.temp.name))
        self.service = self.fixture.service

    def tearDown(self):
        self.temp.cleanup()

    def sync(self):
        return self.service.sync_week("20261109")

    def test_sync_creates_one_group_per_record_and_all_photos(self):
        result = self.sync()
        self.assertEqual(result["groups"], 4)
        self.assertEqual(result["photos_added"], 8)
        groups = self.service.dashboard("20261109")["groups"]
        self.assertEqual(len(groups), 4)
        group = next(item for item in groups if item["id"] == "rec-a")
        self.assertEqual(group["photographer"], "邝俊")
        self.assertEqual(group["status"], "collected")
        self.assertEqual(len(group["photos"]), 2)
        self.assertIn("第二行图说", group["quote"])
        names = [photo["name"] for photo in group["photos"]]
        self.assertEqual(names, ["20261109_邝俊_你看你的我看我的1.jpg", "20261109_邝俊_你看你的我看我的2.jpg"])

    def test_sync_auth_failure_returns_platform_error(self):
        class FailingFeishu:
            def fetch_groups(self, week):
                raise FeishuError("飞书临时认证已过期")

        self.service.feishu = FailingFeishu()
        with self.assertRaises(PlatformError) as caught:
            self.sync()
        self.assertIn("飞书同步失败", str(caught.exception))

    def test_sync_adopts_migrated_group_with_legacy_tokens(self):
        week = self.service._ensure_week(week_for(date(2026, 11, 9)))
        week_dir = Path(week.directory)
        week_dir.mkdir(parents=True, exist_ok=True)
        group = self.service.repo.create_group(
            group_id="grp_legacy",
            week_id=week.id,
            photographer="邝俊",
            title="你看你的我看我的",
            quote="",
            submitted_at=date(2026, 11, 9),
        )
        for index, remote in enumerate(self.fixture.groups[0].attachments, start=1):
            name = build_photo_name(
                title="你看你的我看我的",
                photographer="邝俊",
                submitted_date=date(2026, 11, 9),
                index=index,
                total=2,
                status=STATUS_COLLECTED,
                selected_date=None,
                extension=remote.extension,
            )
            target = week_dir / name
            shutil.copyfile(remote.source, target)
            self.service.repo.add_photo(
                group.id,
                f"legacy:{remote.name}",
                target,
                remote.extension,
                target.stat().st_size,
                remote.name,
            )
        result = self.sync()
        self.assertEqual(result["skipped"], [])
        adopted = self.service.repo.get_group("rec-a")
        self.assertIsNotNone(adopted)
        self.assertEqual(len(adopted.photos), 2)
        self.assertTrue(all(not photo.token.startswith("legacy:") for photo in adopted.photos))

    def test_list_notes_includes_groups_and_images(self):
        self.sync()
        self.service.select_group("rec-a", "2026-11-11")
        self.service.create_note(
            group_ids=["rec-a"],
            title="你看你的我看我的",
            publish_date="2026-11-11",
            dynamic_topics=[],
        )
        listed = self.service.list_notes()
        self.assertEqual(len(listed), 1)
        self.assertEqual(len(listed[0]["groups"]), 1)
        self.assertEqual(len(listed[0]["images"]), 2)

    def test_select_changes_all_group_files_and_keeps_quote(self):
        self.sync()
        result = self.service.select_group("rec-a", "2026-11-11")
        self.assertEqual(result.get("warning"), "")
        group = self.service.repo.get_group("rec-a")
        self.assertEqual(group.status, STATUS_SELECTED)
        self.assertEqual({photo.path.name for photo in group.photos}, {
            "20261109_邝俊_你看你的我看我的1（1111入选）.jpg",
            "20261109_邝俊_你看你的我看我的2（1111入选）.jpg",
        })
        self.assertEqual(group.quote, "第一行图说\n第二行图说 😀")

    def test_unselect_after_close_uses_unselected_suffix(self):
        self.sync()
        self.service.select_group("rec-b", "2026-11-12")
        self.service.unselect_group("rec-b")
        group = self.service.repo.get_group("rec-b")
        self.assertEqual(group.status, "collected")
        self.assertEqual(group.photos[0].path.name, "20261110_陈权_美的感受.png")
        self.service.close_week("20261109")
        group = self.service.repo.get_group("rec-b")
        self.assertEqual(group.status, STATUS_UNSELECTED)
        self.assertEqual(group.photos[0].path.name, "20261110_陈权_美的感受（未入选）.png")

    def test_date_mismatch_warns_but_does_not_block(self):
        self.sync()
        result = self.service.select_group("rec-d", "2026-11-13")
        self.assertIn("日期矛盾", result["warning"])
        group = self.service.repo.get_group("rec-d")
        self.assertEqual(group.selected_date, date(2026, 11, 13))
        self.assertTrue(all("（1113入选）" in photo.path.name for photo in group.photos))

    def test_import_accounts_replaces_mapping_and_backs_up_csv(self):
        self.sync()
        roster = self.fixture.base / "摄影师账号.csv"
        roster.write_text(
            "姓名,小红书号\n邝俊,UESTC_KUANG\n陈权,@chenquan\n李四,请填小红书号\n",
            encoding="utf-8-sig",
        )
        self.service.config.roster_path = roster
        result = self.service.import_accounts()
        self.assertEqual(result["imported"], 2)
        self.assertEqual(self.service.repo.get_account("邝俊"), "UESTC_KUANG")
        self.assertEqual(self.service.repo.get_account("陈权"), "@chenquan")
        self.assertIsNone(self.service.repo.get_account("李四"))
        self.assertTrue(Path(result["backup"]).is_file())

    def test_create_note_topics_draft_and_publish(self):
        self.sync()
        self.service.select_group("rec-a", "2026-11-11")
        note = self.service.create_note(
            group_ids=["rec-a"],
            title="你看你的我看我的",
            publish_date="2026-11-11",
            dynamic_topics=[],
        )
        self.assertEqual(note["day_number"], "day1458")
        self.assertEqual(note["title"], "《你看你的我看我的》")
        self.assertEqual(len(note["images"]), 2)
        self.assertIn("摄影师：邝俊", note["content"])
        self.assertIn("2026年11月11日", note["content"])
        self.assertTrue(note["content"].splitlines()[-1].startswith("#电子科技大学"))

        refreshed = self.service.refresh_topics(note["id"])
        self.assertTrue(refreshed["ok"])
        dynamic = [topic["name"] for topic in refreshed["note"]["topics"]]
        self.assertEqual(dynamic, ["#校园摄影", "#成都", "#逆光"])
        last_line = refreshed["note"]["content"].splitlines()[-1]
        self.assertEqual(last_line, "#电子科技大学 #电子科技大学逆光摄影 #校园摄影 #成都 #逆光")

        draft = self.service.create_draft(note["id"])
        self.assertEqual(draft["draft"]["image_count"], 2)
        self.assertEqual(self.service.note_detail(note["id"])["status"], "draft_filled")
        opened = self.service.open_draft(note["id"])
        self.assertTrue(opened["opened"])
        published = self.service.publish_note(note["id"], confirmed=True)
        self.assertTrue(published["published"])
        note_after = self.service.note_detail(note["id"])
        self.assertEqual(note_after["status"], "published")
        group = self.service.repo.get_group("rec-a")
        self.assertEqual(group.status, "published")
        self.assertTrue(all("（1111入选）" in photo.path.name for photo in group.photos))

    def test_merge_two_groups_same_date(self):
        self.sync()
        self.service.select_group("rec-a", "2026-11-11")
        self.service.select_group("rec-b", "2026-11-11")
        note = self.service.create_note(
            group_ids=["rec-a", "rec-b"],
            title="同一天的两组逆光",
            publish_date="2026-11-11",
            dynamic_topics=["校园摄影"],
        )
        self.assertEqual(len(note["images"]), 3)
        self.assertIn("p1-p2:《你看你的我看我的》", note["content"])
        self.assertIn("p3:《美的感受》", note["content"])
        self.assertEqual(note["groups"][0]["page_start"], 1)
        self.assertEqual(note["groups"][1]["page_end"], 3)

    def test_close_week_blocks_selected_unpublished(self):
        self.sync()
        self.service.select_group("rec-c", "2026-11-12")
        result = self.service.close_week("20261109")
        self.assertFalse(result["ok"])
        self.assertEqual(self.service.repo.get_week("20261109").status, "upload_blocked")

    def test_upload_after_close_and_retry_failure_is_persistent(self):
        self.sync()
        self.service.close_week("20261109")
        week = self.service.queue_upload("20261109", allow_current=True, background=False)
        self.assertEqual(week["status"], "queued")
        task = self.service.process_upload(week["id"])
        self.assertEqual(task["status"], "uploaded")
        self.assertEqual(self.service.repo.get_week("20261109").status, "uploaded")
        self.assertEqual(len(self.fixture.cloud.uploads), 1)
        self.assertEqual(len(self.fixture.cloud.uploads[0][2]), 8)
        self.assertEqual(self.service.repo.get_week("20261109").needs_sync, False)

        failing = Fixture(Path(self.temp.name) / "failing", cloud_fail=True)
        failing.service.sync_week("20261109")
        failing.service.close_week("20261109")
        queued = failing.service.queue_upload("20261109", allow_current=True, background=False)
        failed = failing.service.process_upload(queued["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("模拟上传失败", failed["error"])
        self.assertEqual(failing.service.repo.get_week("20261109").status, "upload_failed")
        listed = failing.service.upload_tasks("20261109")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["status"], "failed")

    def test_scheduler_closes_and_queues(self):
        self.sync()
        scheduler = UploadScheduler(self.service, cooldown_seconds=0)
        now = datetime(2026, 11, 16, 20, 1, tzinfo=timezone(timedelta(hours=8)))
        actions = scheduler.tick(now, background=False)
        self.assertTrue(any(item["action"] == "close" for item in actions))
        self.assertTrue(any(item["action"] == "upload" for item in actions))
        self.assertEqual(self.service.repo.get_week("20261109").status, "closed")
        self.assertEqual(len(self.service.upload_tasks("20261109")), 1)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "web.local.json"
        self.fixture = Fixture(Path(self.temp.name))
        self.auth = AuthManager(self.path, self.fixture.service.db)
        self.password = self.auth.initial_password
        self.assertIsNotNone(self.password)

    def tearDown(self):
        self.temp.cleanup()

    def test_single_session_and_password_change(self):
        token1, _ = self.auth.login(self.password)
        self.assertIsNotNone(self.auth.authenticate(token1))
        token2, _ = self.auth.login(self.password)
        self.assertIsNone(self.auth.authenticate(token1))
        self.assertIsNotNone(self.auth.authenticate(token2))
        with self.assertRaises(AuthError):
            self.auth.login("wrong")
        with self.assertRaises(AuthError):
            self.auth.change_password("wrong", "newpass")
        token3, _ = self.auth.change_password(self.password, "newpass")
        self.assertIsNone(self.auth.authenticate(token2))
        self.assertIsNotNone(self.auth.authenticate(token3))


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = Fixture(Path(self.temp.name))
        self.fixture.service.sync_week("20261109")
        self.auth = AuthManager(Path(self.temp.name) / "web.local.json", self.fixture.service.db)
        self.password = self.auth.initial_password
        self.app = create_app(self.fixture.config, service=self.fixture.service, auth=self.auth, testing=True)
        from fastapi.testclient import TestClient
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def login(self):
        response = self.client.post("/api/login", json={"password": self.password})
        self.assertEqual(response.status_code, 200, response.text)

    def test_requires_login_and_cookie_session(self):
        self.assertEqual(self.client.get("/api/dashboard").status_code, 401)
        self.login()
        response = self.client.get("/api/dashboard?week=20261109")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["week"]["id"], "20261109")
        self.assertEqual(len(response.json()["groups"]), 4)
        group = response.json()["groups"][0]
        photo = group["photos"][0]
        media = self.client.get(photo["url"])
        self.assertEqual(media.status_code, 200)
        self.assertTrue(media.content.startswith(b"fake-image"))

    def test_api_select_create_note_and_settings(self):
        self.login()
        response = self.client.post("/api/groups/rec-b/select", json={"publish_date": "2026-11-12"})
        self.assertEqual(response.status_code, 200, response.text)
        note = self.client.post("/api/notes", json={
            "group_ids": ["rec-b"],
            "title": "美的感受",
            "publish_date": "2026-11-12",
            "dynamic_topics": [],
        })
        self.assertEqual(note.status_code, 200, note.text)
        note_id = note.json()["id"]
        self.assertEqual(self.client.get(f"/api/notes/{note_id}").status_code, 200)
        listed = self.client.get("/api/notes")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()["notes"]), 1)
        self.assertEqual(len(listed.json()["notes"][0]["groups"]), 1)
        settings = self.client.get("/api/settings")
        self.assertEqual(settings.status_code, 200)
        updated = self.client.put("/api/settings", json={"pan123_root": "我的文件/测试/2027-2028"})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["pan123_root"], "我的文件/测试/2027-2028")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_web_publish_requires_explicit_confirmation(self):
        self.login()
        self.client.post("/api/groups/rec-c/select", json={"publish_date": "2026-11-12"})
        note = self.client.post("/api/notes", json={
            "group_ids": ["rec-c"],
            "title": "拼接的世界",
            "publish_date": "2026-11-12",
            "dynamic_topics": [],
        }).json()
        self.assertEqual(self.client.post(f"/api/notes/{note['id']}/publish", json={"confirmed": False}).status_code, 409)
        self.assertEqual(self.client.post(f"/api/notes/{note['id']}/publish", json={"confirmed": True}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
