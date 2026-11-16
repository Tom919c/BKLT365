"""逆光365 推文文案生成。

产出格式（标签**必须**是最后一个非空行，否则上游会静默丢弃）：

    你的笑脸拨动我的琴弦
    摄影师：王璇@wangxuan
    2026年9月15日
    #电子科技大学 #逆光日记

硬约束来自上游 ``publish_pipeline._extract_topic_tags_from_last_line``：
用 ``re.fullmatch(r"#[^\\s#]+", part)`` 逐 token 匹配，所以标签内**不能出现第二个 #**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .naming import PhotoInfo

DEFAULT_TAGS = ("#电子科技大学", "#逆光日记")
PLACEHOLDER_HANDLES = {"请填小红书号", "请填写小红书号", "待补充", "待填写"}

TITLE_MAX_LEN = 20
TITLE_FORBIDDEN = set("《》#\n\r")

# 覆盖常见 emoji / 符号区段（须知明确禁用 emoji）
EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001faff"
    "\U00002600-\U000027bf"
    "\U0001f1e6-\U0001f1ff"
    "\U00002b00-\U00002bff"
    "\U0000fe0f"
    "\U0000200d"
    "]"
)

# 与上游保持一致的标签行校验
_TAG_TOKEN_RE = re.compile(r"#[^\s#]+")


class ComposeError(ValueError):
    """文案不符合平台或社团规则。"""


@dataclass
class Composed:
    """生成好的推文。"""

    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def body(self) -> str:
        """去掉标签行后的正文（上游行为）。"""
        lines = self.content.splitlines()
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and all(_TAG_TOKEN_RE.fullmatch(p) for p in lines[-1].split()):
            lines.pop()
        return "\n".join(lines).strip()


def find_emoji(text: str) -> list[str]:
    return EMOJI_RE.findall(text)


def build_title(title: str) -> str:
    """图名 -> 《图名》。"""
    clean = title.strip().strip("《》")
    if not clean:
        raise ComposeError("图名为空")
    return f"《{clean}》"


def check_title(title: str) -> list[str]:
    """返回标题的问题列表，空列表表示通过。"""
    problems: list[str] = []
    inner = title.strip().strip("《》")
    if len(inner) > TITLE_MAX_LEN:
        problems.append(
            f"图名 {len(inner)} 字，超过小红书标题上限 {TITLE_MAX_LEN} 字"
        )
    bad = TITLE_FORBIDDEN & set(inner)
    if bad:
        problems.append(f"图名含非法字符：{''.join(sorted(bad))}")
    return problems


def build_content(
    photo: PhotoInfo,
    *,
    quote: str = "",
    xhs_handle: str = "",
    tags: tuple[str, ...] | list[str] = DEFAULT_TAGS,
) -> Composed:
    """按逆光365标准生成标题与正文。

    Args:
        photo: 已解析的照片信息。
        quote: 图说（纯文字，禁 emoji）。可为空。
        xhs_handle: 摄影师的小红书号，不含 ``@``。留空则不加 @。
        tags: 话题标签，需带 ``#`` 前缀。

    Raises:
        ComposeError: 文案违反硬约束。
    """
    warnings: list[str] = []

    quote = quote.strip()
    handle = xhs_handle.strip().lstrip("@").strip()
    if handle in PLACEHOLDER_HANDLES:
        handle = ""

    if quote and find_emoji(quote):
        raise ComposeError(f"图说含 emoji，须知明确禁用：{find_emoji(quote)}")

    title = build_title(photo.title)
    problems = check_title(title)
    if problems:
        # 标题超长不阻断发布，仅告警（由主编决定是否精简）
        warnings.extend(problems)

    credit = f"摄影师：{photo.photographer}"
    if handle:
        credit += f"@{handle}"
        warnings.append(
            f"@{handle} 是纯文本，不会真正 @ 到人（上游为 DOM 注入，不产生 mention 节点）"
        )

    tag_line = " ".join(t.strip() for t in tags if t.strip())
    _validate_tag_line(tag_line)

    lines: list[str] = []
    if quote:
        lines.append(quote)
    lines.append(credit)
    lines.append(photo.date_cn)
    lines.append(tag_line)

    content = "\n".join(lines)
    _assert_tags_extractable(content)

    return Composed(
        title=title,
        content=content,
        tags=tag_line.split(),
        warnings=warnings,
    )


def _validate_tag_line(tag_line: str) -> None:
    if not tag_line:
        raise ComposeError("标签行为空")
    parts = tag_line.split()
    for part in parts:
        if not part.startswith("#"):
            raise ComposeError(f"标签缺少 # 前缀：{part!r}")
        if not _TAG_TOKEN_RE.fullmatch(part):
            raise ComposeError(
                f"标签格式非法（含空格或第二个 #）：{part!r}。"
                "注意须知原写法 '#电子科技大学##逆光日记#' 会被上游静默丢弃。"
            )
    if len(parts) > 10:
        raise ComposeError(f"标签 {len(parts)} 个，超过平台上限 10 个")


def _assert_tags_extractable(content: str) -> None:
    """本地复现上游提取逻辑，不通过就早失败。"""
    lines = content.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise ComposeError("正文为空")
    parts = [p for p in lines[-1].split() if p]
    if not parts or not all(_TAG_TOKEN_RE.fullmatch(p) for p in parts):
        raise ComposeError(
            "标签行不是最后一个非空行，上游将无法提取话题标签"
        )
