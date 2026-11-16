"""手机选图 Web MVP 测试。"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from niguang365.collect import scan_inbox  # noqa: E402
from niguang365.publish import draft_mismatch, page_state_is_published  # noqa: E402
from niguang365.publish import PublishResult  # noqa: E402
from niguang365.web import create_app, make_auth_config  # noqa: E402


class WebMVPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "data"
        self.day = self.root / "收件箱" / "20260915"
        self.day.mkdir(parents=True)
        self.first = self.day / "20260915_王璇_晨雾.jpg"
        self.second = self.day / "20260915_王璇_黄昏.jpg"
        Image.new("RGB", (12, 8), "red").save(self.first)
        Image.new("RGB", (8, 12), "blue").save(self.second)
        self.roster = self.root / "摄影师账号.csv"
        self.roster.write_text("摄影师,小红书号\n王璇,wangxuan\n", encoding="utf-8-sig")
        self.config = {
            "root": str(self.root),
            "repo": "unused",
            "python": "python",
            "account": None,
            "post_time": None,
            "roster": str(self.roster),
            "tags": ["#电子科技大学", "#逆光日记"],
            "feishu": {},
            "web": make_auth_config("test-password"),
        }
        self.client = TestClient(create_app(self.config))

    def tearDown(self):
        self.temp.cleanup()

    def login(self):
        response = self.client.post("/api/login", json={"password": "test-password"})
        self.assertEqual(response.status_code, 200, response.text)

    def select_first(self):
        response = self.client.post(
            "/api/selection",
            json={"date": "20260915", "selected": [self.first.name]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_health_and_login_required(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/api/dates").status_code, 401)
        wrong = self.client.post("/api/login", json={"password": "wrong"})
        self.assertEqual(wrong.status_code, 401)
        self.login()
        dates = self.client.get("/api/dates")
        self.assertEqual(dates.status_code, 200)
        self.assertEqual(dates.json()["dates"][0]["count"], 2)

    def test_selection_preview_and_original_bytes_unchanged(self):
        self.login()
        before = hashlib.sha256(self.first.read_bytes()).hexdigest()
        candidates = self.client.get("/api/candidates?date=20260915")
        self.assertEqual(candidates.status_code, 200)
        self.assertEqual(len(candidates.json()["candidates"]), 2)

        result = self.select_first()
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(len(result["previews"]), 1)
        preview = result["previews"][0]
        self.assertEqual(preview["post_title"], "《晨雾》")
        self.assertIn("摄影师：王璇@wangxuan", preview["content"])
        self.assertTrue(preview["content"].endswith("#电子科技大学 #逆光日记"))
        self.assertEqual(hashlib.sha256(self.first.read_bytes()).hexdigest(), before)

        selection = json.loads((self.day / ".niguang365-selection.json").read_text("utf-8"))
        self.assertEqual(selection["selected"], [self.first.name])
        saved_preview = json.loads((self.day / ".niguang365-preview.json").read_text("utf-8"))
        self.assertFalse(saved_preview["published"])
        self.assertEqual(len(scan_inbox(self.root, on_date=date(2026, 9, 15)).selected), 1)

        cleared = self.client.post(
            "/api/selection", json={"date": "20260915", "selected": []}
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["selected_count"], 0)

    def test_media_serves_original_and_rejects_traversal(self):
        self.login()
        candidates = self.client.get("/api/candidates?date=20260915").json()["candidates"]
        image_url = candidates[0]["image_url"]
        response = self.client.get(image_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.first.read_bytes())
        self.assertEqual(self.client.get("/api/dates").status_code, 200)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(image_url).status_code, 401)
        self.login()
        traversal = self.client.get("/media/20260915/%2E%2E%2Fsecret.jpg")
        self.assertEqual(traversal.status_code, 404)

    def test_unknown_candidate_does_not_write_selection(self):
        self.login()
        response = self.client.post(
            "/api/selection",
            json={"date": "20260915", "selected": ["../outside.jpg"]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse((self.day / ".niguang365-selection.json").exists())

    def test_platform_preview_is_always_preview_not_publish(self):
        self.login()
        self.select_first()
        result = PublishResult(
            ok=True,
            preview=True,
            returncode=0,
            stdout="preview filled",
            stderr="",
            command=[],
        )
        with patch("niguang365.web.publish_note", return_value=result) as mocked:
            response = self.client.post(
                "/api/platform-preview",
                json={"date": "20260915", "id": self.first.name},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["preview"])
        self.assertFalse(response.json()["published"])
        kwargs = mocked.call_args.kwargs
        self.assertIs(kwargs["preview"], True)
        self.assertNotIn("auto", kwargs)
        self.assertEqual(Path(kwargs["image"]), self.first)

    def test_platform_preview_requires_saved_selection(self):
        self.login()
        response = self.client.post(
            "/api/platform-preview",
            json={"date": "20260915", "id": self.first.name},
        )
        self.assertEqual(response.status_code, 400)


    def test_platform_draft_status_is_read_only_and_checks_content(self):
        self.login()
        self.select_first()
        matching = {
            "ready": True,
            "images": 1,
            "title": "《晨雾》",
            "content": "摄影师：王璇@wangxuan\n2026年9月15日\n#电子科技大学[话题]#  #逆光日记[话题]#",
        }
        with patch("niguang365.web.get_current_draft", return_value=matching):
            response = self.client.get(
                "/api/platform-draft",
                params={"date": "20260915", "id": self.first.name},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["matches"])
        self.assertFalse((self.day / ".niguang365-published.json").exists())

        with patch("niguang365.web.get_current_draft", return_value={**matching, "title": "《黄昏》"}):
            response = self.client.get(
                "/api/platform-draft",
                params={"date": "20260915", "id": self.first.name},
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["matches"])
        self.assertIn("标题", response.json()["message"])

    def test_platform_publish_requires_confirmation_and_is_persisted(self):
        self.login()
        self.select_first()
        payload = {
            "date": "20260915",
            "id": self.first.name,
            "confirmed": True,
        }
        missing_confirmation = self.client.post(
            "/api/platform-publish",
            json={**payload, "confirmed": False},
        )
        self.assertEqual(missing_confirmation.status_code, 400)

        publish_result = {
            "ok": True,
            "clicked": True,
            "published": True,
            "needs_review": False,
            "page_state": {
                "url": "https://creator.xiaohongshu.com/publish/success",
                "noteLinks": ["https://www.xiaohongshu.com/explore/test-note"],
            },
            "message": "已发布成功。",
        }
        with patch("niguang365.web.publish_current_draft", return_value=publish_result) as mocked:
            response = self.client.post("/api/platform-publish", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["published"])
            self.assertEqual(mocked.call_count, 1)

            repeated = self.client.post("/api/platform-publish", json=payload)
            self.assertEqual(repeated.status_code, 409)
            self.assertEqual(mocked.call_count, 1)

        manifest = json.loads(
            (self.day / ".niguang365-published.json").read_text("utf-8")
        )
        entry = manifest["items"][self.first.name]
        self.assertEqual(entry["title"], "《晨雾》")
        self.assertEqual(entry["note_url"], "https://www.xiaohongshu.com/explore/test-note")

        previews = self.select_first()["previews"]
        self.assertTrue(previews[0]["published"])
        self.assertEqual(
            self.client.post(
                "/api/platform-preview",
                json={"date": "20260915", "id": self.first.name},
            ).status_code,
            409,
        )

    def test_platform_publish_pre_click_failure_does_not_mark_published(self):
        self.login()
        self.select_first()
        result = {
            "ok": False,
            "clicked": False,
            "published": False,
            "needs_review": False,
            "page_state": None,
            "message": "点击发布失败：未找到发布按钮",
        }
        with patch("niguang365.web.publish_current_draft", return_value=result):
            response = self.client.post(
                "/api/platform-publish",
                json={
                    "date": "20260915",
                    "id": self.first.name,
                    "confirmed": True,
                },
            )
        self.assertEqual(response.status_code, 502)
        self.assertFalse((self.day / ".niguang365-published.json").exists())

    def test_platform_publish_ambiguous_click_requires_review(self):
        self.login()
        self.select_first()
        result = {
            "ok": False,
            "clicked": None,
            "published": False,
            "needs_review": True,
            "page_state": {"url": "https://creator.xiaohongshu.com/publish/publish"},
            "message": "结果未确认",
        }
        with patch("niguang365.web.publish_current_draft", return_value=result):
            response = self.client.post(
                "/api/platform-publish",
                json={
                    "date": "20260915",
                    "id": self.first.name,
                    "confirmed": True,
                },
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["needs_review"])
        self.assertFalse(response.json()["published"])

    def test_draft_match_checks_title_body_tags_and_image(self):
        state = {
            "title": "《晨雾》",
            "content": "摄影师：王璇 2026年9月15日 #晨雾[话题]# #电子科技大学",
            "images": 1,
        }
        self.assertEqual(
            draft_mismatch(state, "《晨雾》", "摄影师：王璇\n2026年9月15日\n#电子科技大学"),
            "",
        )
        self.assertIn("标题", draft_mismatch(state, "《黄昏》", "摄影师：王璇"))
        self.assertIn(
            "正文",
            draft_mismatch(state, "《晨雾》", "摄影师：另一人\n2026年9月15日\n#电子科技大学"),
        )
        self.assertIn(
            "话题",
            draft_mismatch(state, "《晨雾》", "摄影师：王璇\n2026年9月15日\n#别的标签"),
        )
        self.assertIn(
            "图片",
            draft_mismatch({**state, "images": 0}, "《晨雾》", "摄影师：王璇\n2026年9月15日"),
        )

    def test_publish_success_page_detection(self):
        self.assertTrue(
            page_state_is_published(
                {"url": "https://creator.xiaohongshu.com/publish/success"}
            )
        )
        self.assertTrue(page_state_is_published({"hasSuccessHint": True}))
        self.assertFalse(
            page_state_is_published(
                {"url": "https://creator.xiaohongshu.com/publish/publish"}
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
