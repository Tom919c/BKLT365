"""旧数据迁移工具（默认只做干跑）。

旧系统按“收件箱/YYYYMMDD”存图，用文件名 `（入选）`、`入选/` 子目录或
`.niguang365-selection.json` 表示入选；新系统按周目录、SQLite 和组状态管理。
本工具只读旧收件箱，把可识别的旧投稿复制到新周目录并写入数据库；不删除、
不覆盖旧文件，遇到同名不同大小或无法唯一归组时报告冲突，交人工处理。

用法：

    python -m niguang365.migrate_legacy --config niguang365/config.json
    python -m niguang365.migrate_legacy --config niguang365/config.json --apply

默认 `--dry-run`；只有显式 `--apply` 才会复制文件和写数据库。迁移前会把
数据库和迁移计划备份到 `<state_root>/backups/migration-<时间>/`。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .core.config import AppConfig, load_config
from .core.db import Database
from .core.domain import build_photo_name, parse_date, week_for
from .core.repo import Repository, new_id
from .naming import FilenameError, is_image, parse_filename


SELECTION_FILENAME = ".niguang365-selection.json"
PUBLISHED_FILENAME = ".niguang365-published.json"
SELECTED_DIRNAMES = {"入选", "selected", "精选"}
DATE_DIR_RE = re.compile(r"^\d{8}$")


@dataclass
class LegacyPhoto:
    source: Path
    relative: str
    submitted_at: date
    photographer: str
    title: str
    base_title: str
    selected: bool
    published: bool
    quote: str = ""
    target: Path | None = None


@dataclass
class LegacyGroup:
    submitted_at: date
    photographer: str
    title: str
    quote: str
    photos: list[LegacyPhoto] = field(default_factory=list)
    selected: bool = False
    published: bool = False
    issues: list[str] = field(default_factory=list)
    blocked: bool = False

    @property
    def key(self) -> tuple[date, str, str]:
        return (self.submitted_at, self.photographer, self.title)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _selection_set(day_dir: Path) -> set[str] | None:
    payload = _load_json(day_dir / SELECTION_FILENAME)
    selected = payload.get("selected")
    if not isinstance(selected, list):
        return None
    return {
        str(item).replace("\\", "/").strip().strip("/")
        for item in selected
        if str(item).strip()
    }


def _published_set(day_dir: Path) -> set[str]:
    payload = _load_json(day_dir / PUBLISHED_FILENAME)
    items = payload.get("items")
    if not isinstance(items, dict):
        return set()
    return {str(key).replace("\\", "/") for key in items}


def _quote_map(day_dir: Path) -> dict[tuple[str, str], str]:
    from .collect import load_submissions

    result: dict[tuple[str, str], str] = {}
    try:
        submissions = load_submissions(day_dir / "submissions.csv")
    except Exception:  # noqa: BLE001
        submissions = []
    for sub in submissions:
        if sub.photographer and sub.title:
            result[(sub.photographer, sub.title)] = sub.quote
    return result


def _normalise_base_titles(records: list[LegacyPhoto]) -> None:
    """把旧系统的 `图名02`、`图名_2` 归回同一组的基名。

    只在同一天、同一摄影师确实存在基名或两个以上同类变体时剥离尾部序号，
    避免把作品名本身就以数字结尾的投稿误合并。
    """
    groups: dict[tuple[date, str], list[LegacyPhoto]] = {}
    for record in records:
        groups.setdefault((record.submitted_at, record.photographer), []).append(record)
    for key, items in groups.items():
        titles = {item.title for item in items}
        stems: dict[str, int] = {}
        for item in items:
            match = re.search(r"(?:_)?(\d{2})$", item.title)
            if not match:
                continue
            stem = item.title[: match.start()].rstrip("_")
            stems[stem] = stems.get(stem, 0) + 1
        for item in items:
            match = re.search(r"(?:_)?(\d{2})$", item.title)
            if not match:
                continue
            stem = item.title[: match.start()].rstrip("_")
            if stem and (stem in titles or stems.get(stem, 0) >= 2):
                item.base_title = stem


def _scan_day(day_dir: Path) -> tuple[list[LegacyPhoto], list[str]]:
    fallback = datetime.strptime(day_dir.name, "%Y%m%d").date()
    selection = _selection_set(day_dir)
    published = _published_set(day_dir)
    quotes = _quote_map(day_dir)
    records: list[LegacyPhoto] = []
    problems: list[str] = []

    candidates: list[tuple[Path, bool]] = []
    for path in sorted(day_dir.rglob("*")):
        if not path.is_file() or not is_image(path):
            continue
        relative_parts = path.relative_to(day_dir).parts
        in_selected_dir = any(part.strip() in SELECTED_DIRNAMES for part in relative_parts[:-1])
        candidates.append((path, in_selected_dir))

    for path, in_selected_dir in candidates:
        try:
            info = parse_filename(path.name, default_date=fallback)
        except FilenameError as exc:
            problems.append(f"{path}: 文件名无法解析，跳过（{exc}）")
            continue
        relative = path.relative_to(day_dir).as_posix()
        if selection is not None:
            selected = relative in selection
        else:
            selected = in_selected_dir or info.selected
        quote = quotes.get((info.photographer, info.title), "") or quotes.get(
            (info.photographer, info.title.rstrip("0123456789")), ""
        )
        records.append(
            LegacyPhoto(
                source=path,
                relative=relative,
                submitted_at=info.taken_on,
                photographer=info.photographer,
                title=info.title,
                base_title=info.title,
                selected=selected,
                published=relative in published,
                quote=quote,
            )
        )
    _normalise_base_titles(records)
    return records, problems


def _build_groups(records: Iterable[LegacyPhoto]) -> tuple[list[LegacyGroup], list[str]]:
    groups: dict[tuple[date, str, str], LegacyGroup] = {}
    problems: list[str] = []
    for record in records:
        key = (record.submitted_at, record.photographer, record.base_title.strip())
        if not key[2]:
            problems.append(f"{record.source}: 作品名为空，跳过")
            continue
        group = groups.get(key)
        if group is None:
            group = LegacyGroup(
                submitted_at=record.submitted_at,
                photographer=record.photographer,
                title=key[2],
                quote=record.quote,
            )
            groups[key] = group
        elif record.quote and not group.quote:
            group.quote = record.quote
        group.photos.append(record)
        group.selected = group.selected or record.selected
        group.published = group.published or record.published
    return list(groups.values()), problems


def _plan_targets(groups: list[LegacyGroup], config: AppConfig) -> list[str]:
    problems: list[str] = []
    occupied_by_week: dict[str, set[Path]] = {}
    for group in groups:
        week = week_for(group.submitted_at)
        week_dir = week.path(config.archive_parent)
        occupied = occupied_by_week.setdefault(week.id, set())
        if any(photo.published for photo in group.photos):
            group.issues.append("旧发布记录：迁移后按“已入选”导入，请人工核对是否已发布")
        photos = sorted(group.photos, key=lambda item: item.source.name)
        for index, photo in enumerate(photos, start=1):
            try:
                name = build_photo_name(
                    title=group.title,
                    photographer=group.photographer,
                    submitted_date=group.submitted_at,
                    index=index,
                    total=len(photos),
                    status="selected" if group.selected else "unselected",
                    selected_date=group.submitted_at if group.selected else None,
                    extension=photo.source.suffix,
                )
            except ValueError as exc:
                problems.append(f"{photo.source}: 生成新文件名失败（{exc}）")
                continue
            target = week_dir / name
            photo.target = target
            if target in occupied:
                problems.append(f"{target}: 同一周出现重名，组已跳过，请人工归组")
                group.blocked = True
            occupied.add(target)
    return problems


def _validate_groups(groups: list[LegacyGroup], config: AppConfig) -> list[str]:
    problems = _plan_targets(groups, config)
    for group in groups:
        names = [photo.target for photo in group.photos if photo.target]
        if len(names) != len(group.photos):
            problems.append(f"{group.photographer}/{group.title}: 有图片未能生成目标名，跳过")
            group.blocked = True
            continue
        for photo in group.photos:
            assert photo.target is not None
            if photo.target.exists() and photo.target.stat().st_size != photo.source.stat().st_size:
                problems.append(
                    f"{photo.target}: 目标已存在且大小不同，不覆盖，组已跳过"
                )
                group.blocked = True
    return problems


def _target_exists_same(photo: LegacyPhoto) -> bool:
    return bool(
        photo.target
        and photo.target.exists()
        and photo.target.stat().st_size == photo.source.stat().st_size
    )


def _apply_groups(
    groups: list[LegacyGroup],
    config: AppConfig,
    *,
    backup_dir: Path,
) -> dict[str, Any]:
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    repo = Repository(db)
    imported_groups = 0
    copied_photos = 0
    skipped_existing = 0
    skipped_groups: list[str] = []
    existing = {(g.submitted_at, g.photographer, g.title) for g in repo.list_groups()}

    for group in groups:
        if group.blocked:
            skipped_groups.append(f"{group.submitted_at} {group.photographer}/{group.title}（预检冲突）")
            continue
        if group.key in existing:
            skipped_groups.append(f"{group.submitted_at} {group.photographer}/{group.title}（数据库中已存在）")
            continue
        week = week_for(group.submitted_at)
        week_dir = week.path(config.archive_parent)
        week_dir.mkdir(parents=True, exist_ok=True)
        repo.ensure_week(week, week_dir)
        group_id = new_id("grp_")
        repo.create_group(
            group_id=group_id,
            week_id=week.id,
            photographer=group.photographer,
            title=group.title,
            quote=group.quote,
            submitted_at=group.submitted_at,
        )
        for photo in group.photos:
            assert photo.target is not None
            if _target_exists_same(photo):
                skipped_existing += 1
            else:
                if photo.target.exists():
                    raise FileExistsError(f"目标文件冲突：{photo.target}")
                shutil.copy2(photo.source, photo.target)
                copied_photos += 1
            repo.add_photo(
                group_id,
                token=f"legacy:{photo.relative}",
                path=photo.target,
                ext=photo.target.suffix,
                size=photo.target.stat().st_size,
                original_name=photo.source.name,
            )
        status = "selected" if group.selected else "unselected"
        repo.update_group(
            group_id,
            status=status,
            selected_date=group.submitted_at.isoformat() if group.selected else None,
            issue="；".join(group.issues) if group.issues else "",
        )
        imported_groups += 1
        existing.add(group.key)

    # 有已入选旧记录的周仍是 collecting，避免迁移后被误判为可直接收束。
    for week_id in {week_for(group.submitted_at).id for group in groups}:
        week = repo.get_week(week_id)
        if not week:
            continue
        has_selected = any(
            g.status == "selected" for g in repo.list_groups(week_id)
        )
        repo.update_week(
            week_id,
            status="collecting" if has_selected else "closed",
            needs_sync=1,
        )

    backup_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "groups": [
            {
                "submitted_at": group.submitted_at.isoformat(),
                "photographer": group.photographer,
                "title": group.title,
                "selected": group.selected,
                "published_from_legacy": group.published,
                "issues": group.issues,
                "photos": [
                    {
                        "source": str(photo.source),
                        "target": str(photo.target),
                        "selected": photo.selected,
                    }
                    for photo in group.photos
                ],
            }
            for group in groups
        ],
        "summary": {
            "groups": imported_groups,
            "copied_photos": copied_photos,
            "skipped_existing": skipped_existing,
            "skipped_groups": skipped_groups,
        },
    }
    (backup_dir / "migration-plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return plan["summary"]


def _collect_sources(root: Path) -> list[Path]:
    inbox = root / "收件箱"
    if not inbox.is_dir():
        return []
    return [
        path
        for path in sorted(inbox.iterdir())
        if path.is_dir() and DATE_DIR_RE.fullmatch(path.name)
    ]


def migrate(
    config: AppConfig,
    *,
    apply: bool = False,
    backup_root: Path | None = None,
) -> dict[str, Any]:
    day_dirs = _collect_sources(config.data_root)
    all_records: list[LegacyPhoto] = []
    problems: list[str] = []
    for day_dir in day_dirs:
        records, day_problems = _scan_day(day_dir)
        all_records.extend(records)
        problems.extend(day_problems)
    groups, group_problems = _build_groups(all_records)
    problems.extend(group_problems)
    validation_problems = _validate_groups(groups, config)
    problems.extend(validation_problems)

    report: dict[str, Any] = {
        "dry_run": not apply,
        "source_days": [str(path) for path in day_dirs],
        "photo_count": len(all_records),
        "group_count": len(groups),
        "selected_groups": sum(1 for group in groups if group.selected),
        "unselected_groups": sum(1 for group in groups if not group.selected),
        "published_legacy_groups": sum(1 for group in groups if group.published),
        "problems": problems,
        "groups": [
            {
                "submitted_at": group.submitted_at.isoformat(),
                "photographer": group.photographer,
                "title": group.title,
                "status": "selected" if group.selected else "unselected",
                "published_from_legacy": group.published,
                "photos": len(group.photos),
                "issues": group.issues,
            }
            for group in groups
        ],
    }
    if not apply:
        return report

    if not day_dirs:
        return report
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = (backup_root or config.backups_root) / f"migration-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if config.db_path.exists():
        shutil.copy2(config.db_path, backup_dir / "before-migration.sqlite3")
    report["summary"] = _apply_groups(groups, config, backup_dir=backup_dir)
    report["backup_dir"] = str(backup_dir)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="逆光365旧收件箱迁移（默认干跑）")
    parser.add_argument("--config", default=None, help="config.json 路径")
    parser.add_argument("--apply", action="store_true", help="实际复制文件并写数据库")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    report = migrate(config, apply=args.apply)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"来源日目录：{len(report['source_days'])} 个")
        print(f"照片：{report['photo_count']}，组：{report['group_count']}，"
              f"入选组：{report['selected_groups']}，未入选组：{report['unselected_groups']}")
        for group in report["groups"]:
            print(f"  {group['submitted_at']} {group['photographer']} / {group['title']} "
                  f"[{group['status']}] {group['photos']} 图")
        for problem in report["problems"]:
            print(f"  冲突：{problem}")
        if report.get("summary"):
            print("已写入：", json.dumps(report["summary"], ensure_ascii=False))
            print("备份：", report.get("backup_dir"))
        else:
            print("干跑完成，未写入任何文件或数据库；确认后加 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
