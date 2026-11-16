# 旧数据迁移与回滚

版本：v2
脚本：`niguang365/migrate_legacy.py`
默认：**干跑，不写文件、不写数据库**

## 1. 迁移范围

脚本只读扫描：

```text
<data_root>/收件箱/YYYYMMDD/
  *.jpg / *.png / *.jpeg / *.webp / *.heic / *.tif / *.tiff / *.bmp
  入选/ selected/ 精选/ 子目录
  .niguang365-selection.json
  .niguang365-published.json
  submissions.csv（可选，取图说）
```

不扫描、不删除：

- 旧 `已归档/YYYY/YYYYMMDD` 日目录（如需迁移，先人工复制到收件箱或另行处理）。
- 浏览器 Cookie、`web.local.json`、配置文件。
- 123 云盘远端文件。

## 2. 识别规则

| 旧来源 | 新含义 |
|---|---|
| 文件名 `YYYYMMDD_摄影师_图名...` | 投稿日、摄影师、作品名 |
| 文件名 `（入选）` | 入选（若存在 selection JSON，以 JSON 为准） |
| `入选/selected/精选` 子目录 | 入选 |
| `.niguang365-selection.json` 的 `selected` | 入选清单，覆盖文件名/目录判定 |
| `.niguang365-published.json` 的 `items` | 曾发布；迁移时标记“旧发布记录，需人工核对” |
| `submissions.csv` 图说 | 组级图说，保留原文/emoji/换行 |

多图归组：

- 同一天、同一摄影师、同一基名视为一组。
- 旧命名 `图名02`、`图名_2` 在存在基名或两个以上同类变体时归回基名。
- 无法可靠归组、同名不同大小、同一周目标重名 → **跳过该组**并列入冲突报告。

## 3. 干跑

```powershell
$env:PYTHONPATH="D:\Code\VSCode\BKLT365"
.\.venv\Scripts\python.exe -m niguang365.migrate_legacy `
  --config niguang365\config.json
```

输出：

- 来源日目录数量
- 照片/组/入选组/未入选组
- 每组状态、图片数
- 冲突列表

干跑不会创建数据库、不会创建周目录、不会复制图片。

JSON 报告：

```powershell
... -m niguang365.migrate_legacy --config niguang365\config.json --json
```

## 4. 正式迁移

**先备份数据库和周目录**（脚本也会自动备份数据库到 `state_root/backups/migration-<时间>/`）：

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = "D:\逆光365图片下载\.niguang365\backups\pre-migration-$stamp"
New-Item -ItemType Directory -Force $backup | Out-Null
Copy-Item "D:\逆光365图片下载\.niguang365\state\niguang365.sqlite3" "$backup\niguang365.sqlite3"
```

执行：

```powershell
.\.venv\Scripts\python.exe -m niguang365.migrate_legacy `
  --config niguang365\config.json --apply
```

脚本行为：

1. 复制旧图到新周目录，不移动、不删除旧图。
2. 目标已存在且大小相同：跳过复制，仍登记。
3. 目标已存在但大小不同：跳过该组，报告冲突，绝不覆盖。
4. 写入 `weeks/groups/photos`；入选组状态 `selected`，未入选组 `unselected`。
5. 有入选组的周状态为 `collecting`（避免误收束）；全部未入选的周为 `closed`。
6. 写入 `needs_sync=1`，提示后续上传。
7. 生成 `migration-plan.json` 到备份目录。

## 5. 迁移后人工处理

- **旧发布记录**：脚本不会伪造 `published` 状态，也不会创建笔记 URL；请主编核对后决定“已发布/重新发布/取消入选”。
- **日期矛盾**：文件名可能使用旧发布日期，需人工确认。
- **冲突组**：按报告中的源路径和目标路径人工归组/改名；不要覆盖旧文件。
- **多图重名**：若旧命名不是 `图名02` 规律，可能被拆成多个组；在 Web 中合并或先在飞书修正。
- **周状态**：确认无误后可在 Web 收束；有 `selected_unpublished` 时会被阻断，这是预期保护。

## 6. 回滚

迁移只复制，不删除旧数据；回滚步骤：

1. 停止 Web 与调度器。
2. 把 `state_root/backups/migration-<时间>/before-migration.sqlite3` 复制回 `db_path`，同时删除/移走 `-wal`、`-shm`。
3. 如需撤销复制出的新周目录，先确认目录内容只包含迁移文件，再整体移到备份区；**不得删除旧 `收件箱`**。
4. 重启 Web，确认工作台显示旧状态。
5. 原始图片始终保留在 `收件箱`，不因回滚丢失。

## 7. 旧链路退役

- `python -m niguang365` 现在启动 Web，不再进入旧 CLI。
- 旧 CLI 仅 `python -m niguang365.cli` 维护使用；不得作为主编发布流程。
- `.niguang365-selection.json` / `.niguang365-published.json` 不再参与新业务判定。
- 迁移报告和数据库备份必须保留到验收结束后。
