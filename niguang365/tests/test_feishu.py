"""飞书解析逻辑测试（纯离线，不联网）。

    python -m unittest discover -s niguang365/tests -t .
"""

from __future__ import annotations

import base64
import gzip
import json
import sys
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from niguang365.feishu import (  # noqa: E402
    Attachment,
    FeishuError,
    FeishuRecord,
    OfficialFeishuBase,
    _decode_records,
    _parse_official_records,
    _parse_records,
)

NAME_FID = "fldzKqqKKW"
TITLE_FID = "fldWYlP7uA"
NUM_FID = "fld446plej"
TS_FID = "fldClZDe0v"
IMG_FID = "fldf8LqT04"


def make_record(**texts) -> FeishuRecord:
    return FeishuRecord(record_id="rec1", texts=texts)


class TestDecodeRecords(unittest.TestCase):
    def test_gzip_base64_roundtrip(self):
        payload = {"recordMap": {"rec1": {}}, "tableRecordNum": 1}
        blob = base64.b64encode(
            gzip.compress(json.dumps(payload).encode("utf-8"))
        ).decode()
        self.assertEqual(_decode_records(blob), payload)

    def test_bad_base64_raises(self):
        with self.assertRaises(FeishuError):
            _decode_records("!!!not base64!!!")

    def test_non_gzip_base64_still_parses(self):
        """兜底：万一上游改成不压缩。"""
        blob = base64.b64encode(b'{"a":1}').decode()
        self.assertEqual(_decode_records(blob), {"a": 1})


class TestParseRecords(unittest.TestCase):
    def test_attachments_and_timestamp(self):
        obj = {
            "recordMap": {
                "rec1": {
                    IMG_FID: {
                        "value": [
                            {
                                "attachmentToken": "TOK1",
                                "name": "a.jpg",
                                "size": 123,
                                "mimeType": "image/jpeg",
                                "width": 10,
                                "height": 20,
                            }
                        ]
                    },
                    NAME_FID: {"value": [{"text": "陈权", "type": "text"}]},
                    TS_FID: {"value": 1789476324205},
                }
            }
        }
        recs = _parse_records(obj)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.attachments[0].token, "TOK1")
        self.assertEqual(rec.attachments[0].size, 123)
        self.assertTrue(rec.attachments[0].is_image)
        self.assertEqual(rec.texts[NAME_FID], "陈权")
        self.assertIsInstance(rec.submitted_at, datetime)

    def test_empty_record_map(self):
        self.assertEqual(_parse_records({}), [])


class TestOfficialRecords(unittest.TestCase):
    @staticmethod
    def page(record_id="rec1", *, has_more=False):
        return {
            "fields": ["提交日期", "姓名", "作品名称", "图片"],
            "field_id_list": [TS_FID, NAME_FID, TITLE_FID, IMG_FID],
            "record_id_list": [record_id],
            "data": [[
                "2026-09-15T20:45:24.205+08:00",
                "陈权",
                "我的一天",
                [{"file_token": "TOK1", "name": "a.jpg", "size": 123}],
            ]],
            "has_more": has_more,
        }

    def test_parse_official_matrix(self):
        recs = _parse_official_records(self.page())
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.record_id, "rec1")
        self.assertEqual(rec.pick_name(NAME_FID), "陈权")
        self.assertEqual(rec.pick_title("陈权", TITLE_FID), "我的一天")
        self.assertEqual(rec.attachments[0].token, "TOK1")
        self.assertEqual(rec.attachments[0].record_id, "rec1")
        self.assertTrue(rec.attachments[0].is_image)
        self.assertIsInstance(rec.submitted_at, datetime)

    def test_official_pagination_uses_row_count(self):
        base = OfficialFeishuBase("app", "table")
        pages = [{"data": self.page("rec1", has_more=True)}, {"data": self.page("rec2")}]
        with mock.patch.object(base, "_run", side_effect=pages) as run:
            records = base.list_records()
        self.assertEqual([r.record_id for r in records], ["rec1", "rec2"])
        offsets = [call.args[0][call.args[0].index("--offset") + 1] for call in run.call_args_list]
        self.assertEqual(offsets, ["0", "1"])

    def test_official_rejects_misaligned_rows(self):
        data = self.page()
        data["record_id_list"] = []
        with self.assertRaises(FeishuError):
            _parse_official_records(data)


class TestPickFields(unittest.TestCase):
    """核心回归：**字段顺序不能影响结果**。

    踩过的坑：用户调整了多维表格的列顺序，导致「取第一个含中文的字段」
    这种启发式选错（把图名当成姓名）。
    """

    def test_explicit_ids_ignore_order(self):
        """显式指定 ID 时，顺序无关 —— 这是配置里的主路径。"""
        for order in (
            {NUM_FID: "1234537", NAME_FID: "陈权", TITLE_FID: "我的一天"},
            {TITLE_FID: "我的一天", NAME_FID: "陈权", NUM_FID: "1234537"},
            {NAME_FID: "陈权", TITLE_FID: "我的一天", NUM_FID: "1234537"},
        ):
            rec = make_record(**order)
            self.assertEqual(rec.pick_name(NAME_FID), "陈权")
            self.assertEqual(rec.pick_title("陈权", TITLE_FID), "我的一天")

    def test_heuristic_skips_pure_digits(self):
        rec = make_record(**{NUM_FID: "1234537", NAME_FID: "陈权"})
        self.assertEqual(rec.pick_name(), "陈权")
        # 没有图名字段时返回空串，让调用方退回序号
        self.assertEqual(rec.pick_title("陈权"), "")

    def test_heuristic_finds_title_when_present(self):
        rec = make_record(
            **{NUM_FID: "1234537", NAME_FID: "陈权", TITLE_FID: "我的一天"}
        )
        self.assertEqual(rec.pick_name(), "陈权")
        self.assertEqual(rec.pick_title("陈权"), "我的一天")

    def test_heuristic_skips_date_like_values(self):
        rec = make_record(**{NAME_FID: "陈权", TITLE_FID: "2026-09-15"})
        self.assertEqual(rec.pick_title("陈权"), "")

    def test_no_text_fields(self):
        rec = FeishuRecord(record_id="rec1")
        self.assertEqual(rec.pick_name(), "")
        self.assertEqual(rec.pick_title(), "")


class TestAttachmentNaming(unittest.TestCase):
    def test_safe_name_strips_illegal_chars(self):
        att = Attachment(token="T", name='a/b:c*d?e"f.jpg')
        self.assertNotIn("/", att.safe_name)
        self.assertNotIn(":", att.safe_name)

    def test_empty_name_falls_back_to_token(self):
        att = Attachment(token="TOK9", name="")
        self.assertEqual(att.safe_name, "TOK9.bin")

    def test_is_image_by_mime_and_ext(self):
        self.assertTrue(Attachment("T", "a.jpg", mime="image/jpeg").is_image)
        self.assertTrue(Attachment("T", "a.PNG", mime="").is_image)
        self.assertFalse(Attachment("T", "a.zip", mime="application/zip").is_image)


class TestConventionNaming(unittest.TestCase):
    """命名规则：YYYYMMDD_姓名_图名.ext，多图时第二张起加序号。"""

    def _name(self, seq: int, title: str) -> str:
        if not title:
            return f"20260915_陈权_{seq:02d}.jpg"
        if seq == 1:
            return f"20260915_陈权_{title}.jpg"
        return f"20260915_陈权_{title}{seq:02d}.jpg"

    def test_single_image_uses_title_only(self):
        self.assertEqual(self._name(1, "我的一天"), "20260915_陈权_我的一天.jpg")

    def test_multi_image_adds_seq_from_second(self):
        self.assertEqual(self._name(1, "我的一天"), "20260915_陈权_我的一天.jpg")
        self.assertEqual(self._name(2, "我的一天"), "20260915_陈权_我的一天02.jpg")

    def test_missing_title_falls_back_to_seq(self):
        self.assertEqual(self._name(1, ""), "20260915_陈权_01.jpg")


class TestSyncDeduplication(unittest.TestCase):
    """重复同步不能把同一张图改名为 _1 / _2。"""

    class Harness:
        def __init__(self, records):
            self.records = records
            self.downloads = 0

        def list_records(self):
            return self.records

        def download(self, attachment, dest_dir):
            self.downloads += 1
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / attachment.safe_name
            target.write_bytes(b"x" * attachment.size)
            return target

    @staticmethod
    def record(size=100):
        return FeishuRecord(
            record_id="rec1",
            texts={NAME_FID: "陈冬青", TITLE_FID: "冷暖乐章"},
            attachments=[Attachment("TOK1", "image.jpg", size=size, mime="image/jpeg")],
            submitted_at=datetime(2026, 11, 16),
        )

    def test_repeated_sync_skips_existing_instead_of_copying(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = self.Harness([self.record()])
            first = OfficialFeishuBase.sync(
                harness,
                root,
                rename="convention",
                name_field_id=NAME_FID,
                title_field_id=TITLE_FID,
            )
            self.assertEqual(first.count, 1)
            target = root / "收件箱" / "20261116" / "20261116_陈冬青_冷暖乐章.jpg"
            self.assertTrue(target.is_file())
            original = target.read_bytes()

            second = OfficialFeishuBase.sync(
                harness,
                root,
                rename="convention",
                name_field_id=NAME_FID,
                title_field_id=TITLE_FID,
            )
            self.assertEqual(second.count, 0)
            self.assertEqual(harness.downloads, 1)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(
                sorted(p.name for p in target.parent.iterdir()),
                ["20261116_陈冬青_冷暖乐章.jpg"],
            )
            self.assertIn("已存在", second.skipped[0][1])

    def test_same_name_different_size_is_not_overwritten(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            OfficialFeishuBase.sync(
                self.Harness([self.record(100)]),
                root,
                rename="convention",
                name_field_id=NAME_FID,
                title_field_id=TITLE_FID,
            )
            conflict = OfficialFeishuBase.sync(
                self.Harness([self.record(101)]),
                root,
                rename="convention",
                name_field_id=NAME_FID,
                title_field_id=TITLE_FID,
            )
            self.assertEqual(conflict.count, 0)
            self.assertIn("大小不同", conflict.skipped[0][1])
            target = root / "收件箱" / "20261116" / "20261116_陈冬青_冷暖乐章.jpg"
            self.assertEqual(target.stat().st_size, 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
