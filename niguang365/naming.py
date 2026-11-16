"""逆光365 文件名规范：解析与生成。

规范：``YYYYMMDD_摄影师_图名（入选）.jpg``
- 分隔符固定为**半角下划线** ``_``
- 入选标记 ``（入选）`` 挂在图名末尾，可缺失
- 图名本身可能含下划线，所以第 3 段之后全部并入图名
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff", ".bmp"}

# 入选标记的四种括号/全半角组合
SELECTED_MARKERS = ("（入选）", "(入选)", "（入选)", "(入选）")
# 兼容 "（1号入选）" 这类带编号的写法
SELECTED_RE = re.compile(r"[（(][^（()）]*入选[^（()）]*[）)]\s*$")

DATE_RE = re.compile(r"^\d{8}$")


class FilenameError(ValueError):
    """文件名不符合逆光365规范。"""


@dataclass(frozen=True)
class PhotoInfo:
    """一张投稿照片的元信息。"""

    taken_on: date
    photographer: str
    title: str
    selected: bool
    ext: str

    @property
    def date_str(self) -> str:
        return self.taken_on.strftime("%Y%m%d")

    @property
    def date_cn(self) -> str:
        d = self.taken_on
        return f"{d.year}年{d.month}月{d.day}日"

    @property
    def year_str(self) -> str:
        return self.taken_on.strftime("%Y")

    def canonical_name(self, *, mark_selected: bool | None = None) -> str:
        """生成规范文件名。``mark_selected`` 默认沿用自身状态。"""
        if mark_selected is None:
            mark_selected = self.selected
        suffix = "（入选）" if mark_selected else ""
        return f"{self.date_str}_{self.photographer}_{self.title}{suffix}{self.ext}"


def _strip_selected_marker(title: str) -> tuple[str, bool]:
    """从图名末尾剥离入选标记，返回 (纯图名, 是否入选)。"""
    for marker in SELECTED_MARKERS:
        if title.endswith(marker):
            return title[: -len(marker)].rstrip(), True

    match = SELECTED_RE.search(title)
    if match:
        return title[: match.start()].rstrip(), True

    return title, False


def parse_filename(
    filename: str | Path,
    *,
    default_date: date | None = None,
) -> PhotoInfo:
    """解析逆光365规范文件名。

    Args:
        filename: 文件名或路径。只取文件名部分。
        default_date: 文件名里没有日期时的兜底日期（例如用当日文件夹名）。

    Raises:
        FilenameError: 段数不足且没有可用的兜底日期。
    """
    name = Path(str(filename)).name
    ext = Path(name).suffix.lower()

    stem = Path(name).stem
    parts = [p.strip() for p in stem.split("_")]

    taken_on: date | None = None
    if parts and DATE_RE.match(parts[0]):
        try:
            taken_on = datetime.strptime(parts[0], "%Y%m%d").date()
        except ValueError as exc:
            raise FilenameError(f"日期段非法：{parts[0]!r}") from exc
        parts = parts[1:]

    if taken_on is None:
        if default_date is None:
            raise FilenameError(
                f"文件名缺少日期段，且未提供兜底日期：{name!r}"
            )
        taken_on = default_date

    if len(parts) < 2:
        raise FilenameError(
            f"文件名段数不足，需要「摄影师_图名」：{name!r}"
        )

    photographer = parts[0]
    raw_title = "_".join(parts[1:])
    title, selected = _strip_selected_marker(raw_title)

    if not photographer:
        raise FilenameError(f"摄影师名为空：{name!r}")
    if not title:
        raise FilenameError(f"图名为空：{name!r}")

    return PhotoInfo(
        taken_on=taken_on,
        photographer=photographer,
        title=title,
        selected=selected,
        ext=ext or ".jpg",
    )


def is_image(path: str | Path) -> bool:
    return Path(str(path)).suffix.lower() in IMAGE_EXTS
