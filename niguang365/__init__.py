"""逆光365 自动化 —— 本地收集 + 小红书发布。

模块划分：
- ``naming``   文件名规范解析/生成
- ``compose``  推文文案生成（含标签硬约束校验）
- ``publish``  调用上游 XiaohongshuSkills 发布
"""

from .naming import FilenameError, PhotoInfo, is_image, parse_filename
from .compose import ComposeError, Composed, build_content, build_title

__all__ = [
    "FilenameError",
    "PhotoInfo",
    "is_image",
    "parse_filename",
    "ComposeError",
    "Composed",
    "build_content",
    "build_title",
]
