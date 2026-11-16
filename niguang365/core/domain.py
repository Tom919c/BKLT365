"""纯业务规则：周、命名、文案与标签。

本模块不访问数据库、不调用平台、不移动文件，所有输入输出都可独立测试。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from .config import DEFAULT_FIXED_TAGS


ILLEGAL_FILENAME = {
    "<": "＜",
    ">": "＞",
    ":": "：",
    '"': "＂",
    "/": "／",
    "\\": "＼",
    "|": "｜",
    "?": "？",
    "*": "＊",
}

STATUS_COLLECTED = "collected"
STATUS_SELECTED = "selected"
STATUS_PUBLISHED = "published"
STATUS_UNSELECTED = "unselected"

WEEK_STATUS_COLLECTING = "collecting"
WEEK_STATUS_CLOSED = "closed"
WEEK_STATUS_UPLOAD_BLOCKED = "upload_blocked"
WEEK_STATUS_UPLOAD_FAILED = "upload_failed"
WEEK_STATUS_UPLOADED = "uploaded"
WEEK_STATUS_CLEANUP_PENDING = "cleanup_pending"


@dataclass(frozen=True, slots=True)
class Week:
    start_date: date
    end_date: date

    @property
    def id(self) -> str:
        return self.start_date.strftime("%Y%m%d")

    @property
    def label(self) -> str:
        return f"{self.start_date:%Y%m%d}-{self.end_date:%m%d}"

    @property
    def directory_name(self) -> str:
        return self.label

    def contains(self, value: date) -> bool:
        return self.start_date <= value <= self.end_date

    def path(self, archive_parent: Path) -> Path:
        return Path(archive_parent) / str(self.start_date.year) / self.directory_name


def week_for(value: date | datetime) -> Week:
    if isinstance(value, datetime):
        value = value.date()
    start = value - timedelta(days=value.weekday())
    return Week(start, start + timedelta(days=6))


def parse_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"无法识别日期：{value!r}，应为 YYYY-MM-DD 或 YYYYMMDD")


def parse_week_id(value: str) -> Week:
    day = parse_date(value)
    return week_for(day)


def day_number(publish_date: date, baseline: str | date = "2022-11-15") -> str:
    baseline_date = parse_date(baseline)
    delta = (publish_date - baseline_date).days + 1
    return f"day{max(delta, 1):04d}"


def sanitize_filename_component(value: str) -> str:
    """替换 Windows 文件名非法字符，保留中文、emoji 与空格。"""
    text = unicodedata.normalize("NFC", str(value))
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    for bad, good in ILLEGAL_FILENAME.items():
        text = text.replace(bad, good)
    # Windows 不允许文件名以点或空格结尾。
    text = text.rstrip(" .")
    if not text:
        raise ValueError("文件名为空")
    return text


def selection_suffix(status: str, selected_date: date | None) -> str:
    if status in (STATUS_SELECTED, STATUS_PUBLISHED, "selecting") or (
        status == "collected" and selected_date is not None
    ):
        if selected_date is None:
            raise ValueError("入选状态缺少入选日期")
        return f"（{selected_date:%m%d}入选）"
    if status == STATUS_UNSELECTED:
        return "（未入选）"
    return ""


def build_photo_name(
    title: str,
    photographer: str,
    submitted_date: date,
    index: int,
    total: int,
    status: str,
    selected_date: date | None,
    extension: str,
) -> str:
    """按任务书生成文件名：投稿日_摄影师_图名[序号]（MMDD入选）.ext。"""
    if not photographer.strip():
        raise ValueError("摄影师姓名不能为空")
    if not title.strip():
        raise ValueError("作品名不能为空")
    if total < 1 or index < 1 or index > total:
        raise ValueError("图片序号超出范围")
    base = sanitize_filename_component(title.strip())
    suffix_index = ""
    if total > 1:
        expected = str(index)
        # 图名本身已以本张序号结尾时不重复追加，例如“冷暖乐章1”+第1张。
        if not (len(base) > len(expected) and base.endswith(expected)):
            suffix_index = expected
    marker = selection_suffix(status, selected_date)
    photographer_part = sanitize_filename_component(photographer.strip())
    stem = f"{submitted_date:%Y%m%d}_{photographer_part}_{base}{suffix_index}{marker}"
    ext = extension if extension.startswith(".") else f".{extension}" if extension else ".jpg"
    return sanitize_filename_component(stem + ext.lower())


@dataclass(frozen=True, slots=True)
class FileItem:
    photo_id: str
    old_path: Path
    extension: str
    index: int


@dataclass(frozen=True, slots=True)
class RenamePlan:
    photo_id: str
    old_path: Path
    new_path: Path


class NamingConflict(RuntimeError):
    def __init__(self, message: str, conflicts: Sequence[Path] = ()):
        super().__init__(message)
        self.conflicts = list(conflicts)


def plan_group_rename(
    *,
    title: str,
    photographer: str,
    submitted_date: date,
    status: str,
    selected_date: date | None,
    items: Sequence[FileItem],
    occupied_paths: Iterable[Path] = (),
    target_dir: Path | None = None,
) -> list[RenamePlan]:
    """一次性计算整组目标文件名，先做全组冲突预检。

    occupied_paths 是“不属于本组”的已存在文件。组内自己的旧路径不算冲突。
    """
    if not items:
        raise ValueError("组内没有图片")
    total = len(items)
    group_old = {Path(item.old_path) for item in items}
    occupied = {Path(path) for path in occupied_paths} | set(group_old)
    targets: list[Path] = []
    plans: list[RenamePlan] = []
    for item in sorted(items, key=lambda value: value.index):
        name = build_photo_name(
            title=title,
            photographer=photographer,
            submitted_date=submitted_date,
            index=item.index,
            total=total,
            status=status,
            selected_date=selected_date,
            extension=item.extension,
        )
        target = Path(target_dir or Path(item.old_path).parent) / name
        if target in targets:
            raise NamingConflict(f"组内目标文件名重复：{target.name}")
        if target.exists() and target not in group_old:
            raise NamingConflict(f"目标文件已存在，拒绝覆盖：{target.name}", [target])
        if target in group_old and target != Path(item.old_path):
            raise NamingConflict(f"组内文件名交叉，拒绝执行：{target.name}", [target])
        if target in occupied and target not in group_old:
            raise NamingConflict(f"目标文件已被其他投稿占用：{target.name}", [target])
        targets.append(target)
        plans.append(RenamePlan(item.photo_id, Path(item.old_path), target))
    return plans


def normalize_title(title: str) -> str:
    text = title.strip()
    if not text:
        raise ValueError("标题不能为空")
    if text.startswith("《") and text.endswith("》"):
        return text
    if text.startswith("《") or text.endswith("》"):
        return f"《{text.strip('《》')}》"
    return f"《{text}》"


def normalize_credit(photographer: str, handle: str | None) -> str:
    if not photographer.strip():
        raise ValueError("摄影师不能为空")
    clean_handle = (handle or "").strip()
    if clean_handle.startswith("@"):
        clean_handle = clean_handle[1:]
    if not clean_handle or clean_handle in {"待确认", "无", "-"}:
        return f"摄影师：{photographer.strip()}"
    return f"摄影师：{photographer.strip()}@{clean_handle}"


def normalize_topics(topics: Iterable[str]) -> list[str]:
    result: list[str] = []
    for topic in topics:
        token = str(topic).strip()
        if not token:
            continue
        if not token.startswith("#"):
            token = "#" + token
        if token in result:
            continue
        result.append(token)
    return result


def topics_line(fixed_tags: Sequence[str], dynamic_topics: Sequence[str], max_topics: int = 10) -> str:
    tags = normalize_topics([*fixed_tags, *dynamic_topics])
    if len(tags) > max_topics:
        raise ValueError(f"话题总数超过上限 {max_topics}")
    return " ".join(tags)


def validate_tags_line(content: str, fixed_tags: Sequence[str] = DEFAULT_FIXED_TAGS, max_topics: int = 10) -> list[str]:
    """标签必须在最后一个非空行，且全部 token 以 # 开头。"""
    lines = [line for line in str(content).splitlines() if line.strip()]
    if not lines:
        raise ValueError("正文为空")
    last = lines[-1].strip()
    tokens = last.split()
    if not tokens or not all(token.startswith("#") and len(token) > 1 for token in tokens):
        raise ValueError("标签必须位于最后一个非空行，且每个话题以 # 开头")
    if len(tokens) > max_topics:
        raise ValueError(f"话题总数超过上限 {max_topics}")
    if len(set(tokens)) != len(tokens):
        raise ValueError("话题存在重复")
    for tag in fixed_tags:
        if tag not in tokens:
            raise ValueError(f"缺少固定标签：{tag}")
    return tokens


def compose_single(
    *,
    title: str,
    photographer: str,
    handle: str | None,
    quote: str,
    publish_date: date,
    day: str | None,
    fixed_tags: Sequence[str],
    dynamic_topics: Sequence[str],
    max_topics: int = 10,
) -> str:
    lines = [
        day or day_number(publish_date),
        normalize_title(title),
        normalize_credit(photographer, handle),
    ]
    quote_clean = str(quote or "").strip("\n")
    if quote_clean.strip():
        lines.append(quote_clean)
    lines.append(f"{publish_date.year}年{publish_date.month}月{publish_date.day}日")
    lines.append(topics_line(fixed_tags, dynamic_topics, max_topics))
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class MergeGroup:
    title: str
    photographer: str
    handle: str | None
    quote: str
    photo_count: int


def page_range(start: int, count: int) -> str:
    if count < 1:
        raise ValueError("每组至少一张图片")
    if count == 1:
        return f"p{start}"
    return f"p{start}-p{start + count - 1}"


def compose_merged(
    *,
    note_title: str,
    groups: Sequence[MergeGroup],
    publish_date: date,
    day: str | None,
    fixed_tags: Sequence[str],
    dynamic_topics: Sequence[str],
    max_topics: int = 10,
) -> tuple[str, list[tuple[int, int]]]:
    if not note_title.strip():
        raise ValueError("合并笔记标题由主编填写")
    if not groups:
        raise ValueError("合并笔记至少包含一组")
    lines = [day or day_number(publish_date), note_title.strip()]
    ranges: list[tuple[int, int]] = []
    cursor = 1
    for group in groups:
        if group.photo_count < 1:
            raise ValueError("每组至少一张图片")
        lines.append(f"{page_range(cursor, group.photo_count)}:{normalize_title(group.title)}")
        lines.append(normalize_credit(group.photographer, group.handle))
        quote_clean = str(group.quote or "").strip("\n")
        if quote_clean.strip():
            lines.append(quote_clean)
        ranges.append((cursor, cursor + group.photo_count - 1))
        cursor += group.photo_count
    lines.append(f"{publish_date.year}年{publish_date.month}月{publish_date.day}日")
    lines.append(topics_line(fixed_tags, dynamic_topics, max_topics))
    return "\n".join(lines), ranges


def extract_last_line_topics(content: str) -> list[str]:
    lines = [line.strip() for line in str(content).splitlines() if line.strip()]
    if not lines:
        return []
    tokens = [token for token in lines[-1].split() if token.startswith("#") and len(token) > 1]
    return tokens


def validate_day(value: str | int, publish_date: date | None = None) -> str:
    if isinstance(value, int):
        return f"day{value:04d}"
    text = str(value).strip()
    if not text:
        if publish_date is None:
            raise ValueError("day 编号不能为空")
        return day_number(publish_date)
    if re.fullmatch(r"day\d{4}", text):
        return text
    if re.fullmatch(r"\d{1,4}", text):
        return f"day{int(text):04d}"
    raise ValueError("day 编号格式应为 dayNNNN，例如 day1460")
