"""本地目录扫描、重命名归档。

目录契约（根目录可配）::

    <root>/
      收件箱/                      ← 飞书自动下载落点，本模块只读不改
        20260915/                  ← 当日文件夹；也兼容图片平铺在 收件箱/ 下
          王璇_谁在树下弹吉他.jpg
          submissions.csv          ← 可选，飞书导出：摄影师,图名,小红书号,图说
          入选/                    ← 可选，主编把选中照片拖进来
      已归档/
        2026/
          20260915/
            20260915_王璇_谁在树下弹吉他.jpg
            manifest.json

「入选」判定支持三种方式，Web 清单存在时以清单为准：
1. 文件名带 ``（入选）``
2. 文件位于名为 ``入选`` / ``selected`` 的子目录下
3. 当日目录的 ``.niguang365-selection.json``（网页保存，不修改原图）
"""

from __future__ import annotations

import csv
import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from .naming import FilenameError, PhotoInfo, is_image, parse_filename

INBOX_DIRNAME = "收件箱"
ARCHIVE_DIRNAME = "已归档"
SELECTED_DIRNAMES = {"入选", "selected", "精选"}
SELECTION_FILENAME = ".niguang365-selection.json"

CSV_FIELD_CANDIDATES = {
    "photographer": ("摄影师", "作者", "姓名", "photographer", "author"),
    "title": ("图名", "作品名", "标题", "title", "name"),
    "handle": ("小红书号", "小红书", "账号", "handle", "xhs", "redbook"),
    "quote": ("图说", "文案", "说明", "quote", "caption", "desc"),
}


class CollectError(RuntimeError):
    """收集/归档阶段出错。"""


@dataclass
class Submission:
    """一条投稿记录（来自 CSV 或映射表）。"""

    photographer: str = ""
    title: str = ""
    handle: str = ""
    quote: str = ""


@dataclass
class Candidate:
    """收件箱里一张待处理的照片。"""

    path: Path
    info: PhotoInfo
    selected: bool
    submission: Submission = field(default_factory=Submission)
    problems: list[str] = field(default_factory=list)

    @property
    def handle(self) -> str:
        return self.submission.handle

    @property
    def quote(self) -> str:
        return self.submission.quote


# --------------------------------------------------------------------------
# CSV / 映射表
# --------------------------------------------------------------------------


def _match_column(header: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {h.strip().lower(): h for h in header}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def load_submissions(csv_path: str | Path) -> list[Submission]:
    """读取飞书导出的投稿表。列名按常见叫法模糊匹配。"""
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        return []

    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = csv_path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise CollectError(f"无法解码投稿表：{csv_path}")

    rows = list(csv.DictReader(text.splitlines()))
    if not rows:
        return []

    header = list(rows[0].keys())
    col = {
        key: _match_column(header, cands)
        for key, cands in CSV_FIELD_CANDIDATES.items()
    }

    out: list[Submission] = []
    for row in rows:
        sub = Submission()
        for key, column in col.items():
            if column:
                setattr(sub, key, (row.get(column) or "").strip())
        if sub.photographer or sub.title:
            out.append(sub)
    return out


def load_roster(path: str | Path) -> dict[str, str]:
    """读取「摄影师 -> 小红书号」映射（CSV 或 JSON）。"""
    path = Path(path)
    if not path.is_file():
        return {}

    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k).strip(): str(v).strip() for k, v in data.items()}

    out: dict[str, str] = {}
    for sub in load_submissions(path):
        if sub.photographer and sub.handle:
            out[sub.photographer] = sub.handle
    return out


def _build_index(subs: list[Submission]) -> dict[tuple[str, str], Submission]:
    return {(s.photographer, s.title): s for s in subs}


def _lookup(
    info: PhotoInfo,
    index: dict[tuple[str, str], Submission],
    roster: dict[str, str],
) -> Submission:
    """按 (摄影师, 图名) 精确匹配；失败则退回摄影师级映射。"""
    hit = index.get((info.photographer, info.title))
    if hit:
        return hit

    handle = roster.get(info.photographer, "")
    if handle:
        return Submission(photographer=info.photographer, handle=handle)
    return Submission()


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------


def _iter_images(folder: Path):
    for path in sorted(folder.rglob("*")):
        if path.is_file() and is_image(path):
            yield path


def _is_selected_path(path: Path, inbox: Path) -> bool:
    try:
        rel = path.relative_to(inbox)
    except ValueError:
        rel = path
    return any(part.strip() in SELECTED_DIRNAMES for part in rel.parts[:-1])


def candidate_id(path: str | Path, scan_root: str | Path) -> str:
    """Web 清单使用的相对路径 ID，统一为正斜杠。"""
    return Path(path).relative_to(Path(scan_root)).as_posix()


def _normalise_candidate_id(value: str) -> str:
    text = str(value).replace("\\", "/").strip().strip("/")
    pure = PurePosixPath(text)
    if not text or pure.is_absolute() or ".." in pure.parts:
        raise CollectError(f"非法候选文件 ID：{value!r}")
    return text


def load_web_selection(scan_root: str | Path) -> set[str] | None:
    """读取 Web 保存的入选清单；文件不存在时返回 ``None``。

    清单存在时覆盖文件名/入选目录的判定，让主编可以在网页上取消入选，
    同时完全不改动原始图片文件。
    """
    path = Path(scan_root) / SELECTION_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectError(f"入选清单无法读取：{path}（{exc}）") from exc
    selected = payload.get("selected")
    if not isinstance(selected, list):
        raise CollectError(f"入选清单缺少 selected 数组：{path}")
    return {_normalise_candidate_id(item) for item in selected}


def save_web_selection(
    root: str | Path, *, on_date: date, selected: list[str]
) -> Path:
    """原子保存 Web 入选清单，返回清单路径。"""
    root = Path(root)
    inbox = root / INBOX_DIRNAME
    day_dir = inbox / on_date.strftime("%Y%m%d")
    scan_root = day_dir if day_dir.is_dir() else inbox
    scan_root.mkdir(parents=True, exist_ok=True)
    target = scan_root / SELECTION_FILENAME
    payload = {
        "version": 1,
        "date": on_date.strftime("%Y%m%d"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "web",
        "selected": sorted({_normalise_candidate_id(x) for x in selected}),
    }
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(target)
    return target


@dataclass
class ScanResult:
    """一次收件箱扫描的结果。"""

    candidates: list[Candidate] = field(default_factory=list)
    unparsed: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def selected(self) -> list[Candidate]:
        return [c for c in self.candidates if c.selected]

    def __len__(self) -> int:
        return len(self.candidates)


def scan_inbox(
    root: str | Path,
    *,
    on_date: date,
    roster_path: str | Path | None = None,
) -> ScanResult:
    """扫描收件箱，返回当日所有候选照片。

    Args:
        root: 项目根目录。
        on_date: 目标日期。用于确定当日文件夹名与日期兜底。
        roster_path: 可选的「摄影师 -> 小红书号」映射文件。

    Raises:
        CollectError: 找不到收件箱目录。
    """
    root = Path(root)
    inbox = root / INBOX_DIRNAME
    if not inbox.is_dir():
        raise CollectError(f"收件箱不存在：{inbox}")

    day_str = on_date.strftime("%Y%m%d")
    day_dir = inbox / day_str
    scan_root = day_dir if day_dir.is_dir() else inbox

    submissions = load_submissions(scan_root / "submissions.csv")
    index = _build_index(submissions)
    roster = load_roster(roster_path) if roster_path else {}
    # roster 兜底：收件箱根目录放一份也行
    if not roster:
        roster = load_roster(inbox / "摄影师账号.csv")
    web_selection = load_web_selection(scan_root)

    result = ScanResult()
    for path in _iter_images(scan_root):
        try:
            info = parse_filename(path.name, default_date=on_date)
        except FilenameError as exc:
            result.unparsed.append((path, str(exc)))
            continue

        problems: list[str] = []
        selected = info.selected or _is_selected_path(path, scan_root)
        if web_selection is not None:
            selected = candidate_id(path, scan_root) in web_selection
        sub = _lookup(info, index, roster)
        if not sub.handle:
            problems.append("缺小红书号（CSV 无记录且映射表未命中）")

        result.candidates.append(
            Candidate(
                path=path,
                info=info,
                selected=selected,
                submission=sub,
                problems=problems,
            )
        )

    return result


# --------------------------------------------------------------------------
# 归档
# --------------------------------------------------------------------------


@dataclass
class ArchiveReport:
    day_dir: Path
    manifest: Path
    moved: list[tuple[str, str]]
    skipped: list[tuple[str, str]]


def archive(
    root: str | Path,
    *,
    on_date: date,
    candidates: list[Candidate],
    copy: bool = True,
    mark_selected: bool = True,
) -> ArchiveReport:
    """把候选照片按规范重命名并归档到 ``已归档/<年>/<YYYYMMDD>/``。

    Args:
        copy: True 复制（保留收件箱原文件），False 移动。
        mark_selected: True 给入选照片加上 ``（入选）`` 标记。
    """
    root = Path(root)
    day_str = on_date.strftime("%Y%m%d")
    day_dir = root / ARCHIVE_DIRNAME / on_date.strftime("%Y") / day_str
    day_dir.mkdir(parents=True, exist_ok=True)

    moved: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    records: list[dict] = []

    for cand in candidates:
        canonical = cand.info.canonical_name(
            mark_selected=cand.selected if mark_selected else False
        )
        target = day_dir / canonical
        if target.exists():
            skipped.append((cand.path.name, "目标已存在"))
            continue

        try:
            if copy:
                shutil.copy2(cand.path, target)
            else:
                shutil.move(str(cand.path), str(target))
        except OSError as exc:
            skipped.append((cand.path.name, f"IO 失败：{exc}"))
            continue

        moved.append((cand.path.name, canonical))
        record = asdict(cand.info)
        record["taken_on"] = cand.info.taken_on.isoformat()
        record["selected"] = cand.selected
        record["source"] = str(cand.path)
        record["archived"] = canonical
        record["handle"] = cand.handle
        record["quote"] = cand.quote
        record["problems"] = cand.problems
        records.append(record)

    manifest = day_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "date": day_str,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "count": len(records),
                "items": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return ArchiveReport(
        day_dir=day_dir, manifest=manifest, moved=moved, skipped=skipped
    )
