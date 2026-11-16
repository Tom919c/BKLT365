"""核心逻辑测试。用 stdlib unittest，无额外依赖。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from niguang365.compose import (  # noqa: E402
    ComposeError,
    build_content,
    build_title,
)
from niguang365.logincheck import (  # noqa: E402
    CREATOR_UID_COOKIE,
    _creator_dom_confirms_login,
)
from niguang365.naming import (  # noqa: E402
    FilenameError,
    parse_filename,
)

from niguang365.publish import _draft_has_content  # noqa: E402

UPSTREAM_SCRIPTS = Path(r"D:\Code\VSCode\BKLT365\XiaohongshuSkills\scripts")


class TestNaming(unittest.TestCase):
    def test_standard_selected(self):
        info = parse_filename("20260915_王璇_谁在树下弹吉他（入选）.jpg")
        self.assertEqual(info.date_str, "20260915")
        self.assertEqual(info.photographer, "王璇")
        self.assertEqual(info.title, "谁在树下弹吉他")
        self.assertTrue(info.selected)
        self.assertEqual(info.ext, ".jpg")

    def test_not_selected(self):
        info = parse_filename("20260915_王璇_谁在树下弹吉他.jpg")
        self.assertFalse(info.selected)
        self.assertEqual(info.title, "谁在树下弹吉他")

    def test_halfwidth_parens(self):
        info = parse_filename("20260915_王璇_谁在树下弹吉他(入选).jpg")
        self.assertTrue(info.selected)
        self.assertEqual(info.title, "谁在树下弹吉他")

    def test_numbered_selected_marker(self):
        info = parse_filename("20220912_李四_晨雾（1号入选）.jpg")
        self.assertTrue(info.selected)
        self.assertEqual(info.title, "晨雾")

    def test_title_containing_underscore(self):
        info = parse_filename("20260915_王璇_谁在_树下弹吉他（入选）.jpg")
        self.assertEqual(info.title, "谁在_树下弹吉他")
        self.assertTrue(info.selected)

    def test_date_fallback(self):
        info = parse_filename(
            "王璇_谁在树下弹吉他.jpg", default_date=date(2026, 9, 15)
        )
        self.assertEqual(info.date_str, "20260915")

    def test_missing_date_without_fallback_raises(self):
        with self.assertRaises(FilenameError):
            parse_filename("王璇_谁在树下弹吉他.jpg")

    def test_too_few_parts_raises(self):
        with self.assertRaises(FilenameError):
            parse_filename("20260915_王璇.jpg")

    def test_empty_title_raises(self):
        with self.assertRaises(FilenameError):
            parse_filename("20260915_王璇_（入选）.jpg")

    def test_canonical_name_roundtrip(self):
        info = parse_filename("20260915_王璇_谁在树下弹吉他.jpg")
        self.assertEqual(
            info.canonical_name(mark_selected=True),
            "20260915_王璇_谁在树下弹吉他（入选）.jpg",
        )
        self.assertEqual(
            info.canonical_name(mark_selected=False),
            "20260915_王璇_谁在树下弹吉他.jpg",
        )

    def test_date_cn(self):
        info = parse_filename("20260915_王璇_谁在树下弹吉他.jpg")
        self.assertEqual(info.date_cn, "2026年9月15日")


class TestCompose(unittest.TestCase):
    def setUp(self):
        self.photo = parse_filename("20260915_王璇_谁在树下弹吉他（入选）.jpg")

    def test_title_wrapped(self):
        self.assertEqual(build_title("谁在树下弹吉他"), "《谁在树下弹吉他》")

    def test_full_content_shape(self):
        c = build_content(
            self.photo, quote="你的笑脸拨动我的琴弦", xhs_handle="wangxuan"
        )
        self.assertEqual(c.title, "《谁在树下弹吉他》")
        self.assertEqual(
            c.content,
            "你的笑脸拨动我的琴弦\n"
            "摄影师：王璇@wangxuan\n"
            "2026年9月15日\n"
            "#电子科技大学 #逆光日记",
        )
        self.assertEqual(c.tags, ["#电子科技大学", "#逆光日记"])

    def test_body_excludes_tags(self):
        c = build_content(self.photo, quote="你的笑脸拨动我的琴弦")
        self.assertNotIn("#电子科技大学", c.body)
        self.assertIn("摄影师：王璇", c.body)

    def test_handle_at_prefix_stripped(self):
        c = build_content(self.photo, quote="x", xhs_handle="@wangxuan")
        self.assertIn("摄影师：王璇@wangxuan", c.content)

    def test_no_handle_ok(self):
        c = build_content(self.photo, quote="x")
        self.assertIn("摄影师：王璇\n", c.content)
        self.assertNotIn("@", c.content)

    def test_placeholder_handle_not_written_to_content(self):
        c = build_content(self.photo, quote="x", xhs_handle="请填小红书号")
        self.assertIn("摄影师：王璇\n", c.content)
        self.assertNotIn("@", c.content)

    def test_old_tag_format_rejected(self):
        """须知原写法会被上游静默丢弃，这里必须直接拒绝。"""
        with self.assertRaises(ComposeError):
            build_content(
                self.photo,
                quote="x",
                tags=("#电子科技大学##逆光日记#",),
            )

    def test_emoji_in_quote_rejected(self):
        with self.assertRaises(ComposeError):
            build_content(self.photo, quote="你的笑脸拨动我的琴弦 😊")

    def test_empty_quote_ok(self):
        c = build_content(self.photo)
        self.assertTrue(c.content.startswith("摄影师：王璇"))

    def test_long_title_warns_not_raises(self):
        photo = parse_filename(
            "20260915_王璇_" + "很长的图名" * 5 + ".jpg"
        )
        c = build_content(photo, quote="x")
        self.assertTrue(any("超过" in w for w in c.warnings))


class TestDraftGuard(unittest.TestCase):
    def test_empty_draft_allowed(self):
        self.assertFalse(_draft_has_content({"images": 0, "title": "", "content": ""}))

    def test_existing_images_or_text_rejected(self):
        self.assertTrue(_draft_has_content({"images": 2, "title": "", "content": ""}))
        self.assertTrue(_draft_has_content({"images": 0, "title": "旧稿", "content": ""}))
        self.assertTrue(_draft_has_content({"images": 0, "title": "", "content": "旧正文"}))


class TestLoginCheck(unittest.TestCase):
    def test_dom_and_uid_confirm_login_without_old_token(self):
        probe = {
            "accountId": "123",
            "nickname": "tester",
            "hasPublishEntry": True,
            "onLoginPage": False,
        }
        self.assertTrue(_creator_dom_confirms_login(probe, {CREATOR_UID_COOKIE}))

    def test_login_page_always_rejected(self):
        probe = {
            "accountId": "123",
            "hasPublishEntry": True,
            "onLoginPage": True,
        }
        self.assertFalse(_creator_dom_confirms_login(probe, {CREATOR_UID_COOKIE}))

    def test_publish_entry_without_identity_is_not_enough(self):
        probe = {"hasPublishEntry": True, "onLoginPage": False}
        self.assertFalse(_creator_dom_confirms_login(probe, set()))

    def test_account_identity_can_replace_uid_cookie(self):
        probe = {
            "accountId": "123",
            "hasPublishEntry": True,
            "onLoginPage": False,
        }
        self.assertTrue(_creator_dom_confirms_login(probe, set()))


@unittest.skipUnless(
    UPSTREAM_SCRIPTS.is_dir(), "上游仓库不存在，跳过集成测试"
)
class TestUpstreamIntegration(unittest.TestCase):
    """把生成的正文喂给上游真实的标签提取函数。"""

    def test_generated_content_passes_upstream_extraction(self):
        sys.path.insert(0, str(UPSTREAM_SCRIPTS))
        from publish_pipeline import _extract_topic_tags_from_last_line as extract

        photo = parse_filename("20260915_王璇_谁在树下弹吉他（入选）.jpg")
        c = build_content(
            photo, quote="你的笑脸拨动我的琴弦", xhs_handle="wangxuan"
        )
        body, tags = extract(c.content)

        self.assertEqual(tags, ["#电子科技大学", "#逆光日记"])
        self.assertNotIn("#电子科技大学", body)
        self.assertIn("摄影师：王璇@wangxuan", body)

    def test_old_format_yields_no_tags(self):
        """留档证据：须知原格式在上游拿不到任何标签。"""
        sys.path.insert(0, str(UPSTREAM_SCRIPTS))
        from publish_pipeline import _extract_topic_tags_from_last_line as extract

        old = (
            "你的笑脸拨动我的琴弦\n"
            "摄影师：王璇\n"
            "#电子科技大学##逆光日记#\n"
            "2026年9月15日"
        )
        body, tags = extract(old)
        self.assertEqual(tags, [])
        self.assertIn("#电子科技大学##逆光日记#", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
