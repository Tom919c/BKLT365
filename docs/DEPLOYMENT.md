# 配置、部署与运维

版本：v2
最后验证：2026-11-17
适用：Windows 10/11 工作室电脑（本地部署）

> 2026-11-17 起，当前开发电脑经用户明确授权，已作为 `365.niguang.me` 的生产入口运行 v2，并配置了当前用户登录自启项。本文中的系统设置仅针对本机；其他电脑仍按工作室部署清单执行。

## 1. 运行前提

| 组件 | 要求 |
|---|---|
| Windows | 10/11，建议常开、接通电源 |
| Python | 3.11+；推荐 3.13 虚拟环境 |
| 依赖 | `niguang365/requirements.txt` |
| Chrome | 独立 `XiaohongshuProfiles\default`，固定 CDP 端口 9222 |
| 123 云盘 | 项目 Chrome 中已登录；年度根目录存在 |
| 飞书 | 只读应用凭据/表格坐标；不需要写权限 |
| 公网 | Cloudflare Tunnel：`365.niguang.me → 127.0.0.1:8501` |

安装：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r niguang365\requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

如果使用已有虚拟环境，把下文命令中的解释器替换为该环境的 `python.exe`。

## 2. 配置

### 2.1 生成配置文件

```powershell
Copy-Item niguang365\config.example.json niguang365\config.json
```

若 `config.json` 已存在，先备份再按需合并，**不要覆盖**现有真实配置。`config.example.json` 只含占位符；真实凭据只写入本机 `config.json`。`web.local.json` 由 `--init-auth` 生成，不要手工伪造。

### 2.2 关键字段

| 字段 | 说明 | 默认/示例 |
|---|---|---|
| `data_root` | 数据根 | `D:\逆光365图片下载` |
| `archive_parent` | 周目录父路径 | `<data_root>\已归档` |
| `state_root` | 状态/备份/清单根 | `<data_root>\.niguang365` |
| `db_path` | SQLite 路径 | `<state_root>\state\niguang365.sqlite3` |
| `roster` | 摄影师账号 CSV | `<data_root>\摄影师账号.csv` |
| `repo` | 上游 `XiaohongshuSkills` 路径 | 仓库内 `XiaohongshuSkills` |
| `fixed_tags` | 固定品牌标签 | `#电子科技大学`、`#电子科技大学逆光摄影` |
| `dynamic_topic_count` | 动态话题数量 | 3（总数 5—6，上限 10） |
| `pan123.root_path` | 123 年度根路径 | `我的文件/逆光摄影工作室/逆光日记365/2026-2027` |
| `upload.weekday/hour/minute` | 自动收束/上传 | `0 / 20 / 0`（周一 20:00） |
| `feishu.*` | 飞书字段 ID | 姓名 `fldzKqqKKW`、图名 `fldWYlP7uA`、日期 `fldClZDe0v`、图片 `fldf8LqT04`、图说 `fldI8aQytg` |
| `feishu.cli_path` | `lark-cli` 可执行文件绝对路径 | 不填则用 PATH 中的 `lark-cli`；当前生产机为 `D:\逆光365图片下载\.niguang365\tools\lark-cli.exe` |
| `cdp_port` | Chrome CDP | 9222 |
| `account` | 账号备注 | 可选 |

2027 年切换年度目录：在 Web“设置 → 123 年度根目录”改 `2027-2028`，新任务立即生效；不需要改代码。

### 2.3 参数校验边界

- `pan123_root` 不能为空。
- 上传星期 0—6；小时 0—23；分钟 0—59。
- 动态话题 1—10；总数上限 2—10。
- `fixed_tags` 至少 1 个、最多 10 个，自动补 `#`。
- `publish_mode` 只能是 `test`/`official`。
- 正式发布还需 `official_publish_authorized=true`；测试模式强制走测试账号逻辑。

## 3. 首次启动

```powershell
$env:PYTHONPATH="D:\Code\VSCode\BKLT365"
.\.venv\Scripts\python.exe -m niguang365.webapp --init-auth
.\.venv\Scripts\python.exe -m niguang365.webapp --host 127.0.0.1 --port 8501
```

- `--init-auth` 首次生成 `niguang365/web.local.json`，终端只显示一次初始共享密码；已存在时不覆盖。
- 也可以 `python -m niguang365`，现在默认启动 Web；旧 CLI 需显式 `python -m niguang365.cli`。
- 打开 `http://127.0.0.1:8501`，用共享密码登录。

开发期可加 `--no-scheduler` 关闭周任务；生产不要关闭。

## 4. Chrome / 小红书 CDP

必须使用**独立配置目录**，不要占用主编日常 Chrome：

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --user-data-dir="$env:LOCALAPPDATA\Google\Chrome\XiaohongshuProfiles\default" `
  --remote-debugging-port=9222 `
  --remote-debugging-address=127.0.0.1
```

验证：

```powershell
Invoke-RestMethod http://127.0.0.1:9222/json/version
Invoke-RestMethod http://127.0.0.1:9222/json/list
```

- 首次需人工扫码登录小红书创作者中心与 123 云盘；登录态属于用户，不由 Agent 代办。
- 服务端只通过 CDP 操作该 Chrome；不要修改 `--remote-debugging-address` 为公网。
- 若 CDP 断开，Web 的草稿/发布应明确报错，不伪造成功。

## 5. 123 云盘

- 在项目 Chrome 中登录 123 云盘。
- 年度根路径必须已存在：`我的文件/逆光摄影工作室/逆光日记365/2026-2027`。
- 系统按周目录名上传整个文件夹，逐文件回读名称/大小/结构。
- 同名周目录存在时只补缺失文件，不重复创建副本；同名不同大小报告冲突，不覆盖。
- 跨周改名时先上传新文件并核验，再只删除明确对应的旧文件；清理失败保留 `cleanup_pending`。

## 6. Cloudflare Tunnel

当前生产机（本机）已由 `Cloudflared` 服务承载；新机部署时执行：

```powershell
cloudflared tunnel login
cloudflared tunnel create niguang365
cloudflared tunnel route dns niguang365 365.niguang.me
```

配置 `config.yml`：

```yaml
tunnel: <tunnel-id>
credentials-file: C:\Users\<user>\.cloudflared\<tunnel-id>.json

ingress:
  - hostname: 365.niguang.me
    service: http://127.0.0.1:8501
  - service: http_status:404
```

安装为 Windows 服务：

```powershell
cloudflared service install
```

验收：外网手机打开 `https://365.niguang.me`，登录后可见工作台；直接访问公网 IP:8501 应超时。

## 7. 开机自启与电源（当前生产机已执行）

2026-11-17 经用户明确授权，本机使用**当前用户登录自启项**管理 Web 与项目 Chrome。计划任务 `Niguang365-Stack` 因当前执行令牌 `Access is denied`（`HRESULT 0x80070005`）未能创建，因此实际入口改为 HKCU Run；仍然是用户级的一套总开关。

| 项目 | 当前值 |
|---|---|
| 自启注册值 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Niguang365Stack` |
| 自启命令 | `wscript.exe "D:\Code\VSCode\BKLT365\deploy\start-stack.vbs"` |
| 启动器 | `deploy/start-stack.vbs` 隐藏调用 `start-web.ps1` 与 `start-chrome.ps1` |
| Web | `http://127.0.0.1:8501`，日志在 `D:\逆光365图片下载\.niguang365\logs` |
| Chrome | `%LOCALAPPDATA%\Google\Chrome\XiaohongshuProfiles\default`，CDP `127.0.0.1:9222` |
| 自启总开关（设置页） | 已置为 `true`（仅记录部署状态，实际入口仍是本机 HKCU Run） |

启动脚本行为：

- `start-web.ps1` 先请求 `/api/health`；已健康则退出 0，不重复启动。
- 8501 已监听但健康失败时退出 1，不盲目再起一个进程。
- 否则隐藏启动 `python -m niguang365.webapp --host 127.0.0.1 --port 8501 --config ...`，日志按启动时间写入上述 logs 目录。
- `start-chrome.ps1` 先检查 `/json/version`；9222 已健康则退出 0；否则用独立 profile 和 CDP 参数启动 Chrome。

> 飞书 CLI 的临时认证：`start-web.ps1` 会读取 `state_root\tools\cli-env.json` 注入 `lark-cli` 运行环境，这只是本机过渡验证方案。豆包 Agent 下发的 `DOUBAO_OFFICE_CLI_TOKEN` 是一次性/短时 token，不能作为长期生产认证；生产环境必须配置持久飞书应用凭据或已完成 OAuth 的 CLI profile。不要提交、打印或复制该文件中的 token。

> 重要：`start-web.ps1` / `start-chrome.ps1` 含中文路径，文件必须保存为 **UTF-8 with BOM**。Windows PowerShell 5.1 会把无 BOM 的 UTF-8 中文路径读成乱码，表现为“Web 能起来，但日志目录不是预期路径”。

停用自启：

```powershell
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'Niguang365Stack'
```

验证方式：注销并重新登录，或手动执行 `wscript.exe "D:\Code\VSCode\BKLT365\deploy\start-stack.vbs"` 后检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8501/api/health
Invoke-RestMethod http://127.0.0.1:9222/json/version
Get-Content 'D:\逆光365图片下载\.niguang365\logs\autostart.log' -Tail 5
```

> 自启项只在当前用户登录后触发；机器重启但无人登录时，Web 不会启动。Cloudflared 是 Windows 服务，随系统启动。电源策略仍建议“永不睡眠、合盖不睡眠、接通电源”。

## 8. 备份

建议每日/每次大改前：

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = "D:\逆光365图片下载\.niguang365\backups\$stamp"
New-Item -ItemType Directory -Force $backup | Out-Null
Copy-Item "D:\逆光365图片下载\.niguang365\state\niguang365.sqlite3" "$backup\niguang365.sqlite3"
Copy-Item "D:\Code\VSCode\BKLT365\niguang365\config.json" "$backup\config.json"
```

- 同时备份 `已归档` 周目录；原始图片不压缩。
- 恢复时先停 Web，替换数据库，保留 `-wal`/`-shm` 同目录文件；再启动。
- 迁移脚本会自动把数据库和迁移计划备份到 `state_root/backups/migration-<时间>/`。

## 9. 健康检查与故障恢复

| 现象 | 检查 | 处理 |
|---|---|---|
| 页面打不开 | `Invoke-RestMethod http://127.0.0.1:8501/api/health` | 看 Web 进程/端口；重新启动 |
| 同步飞书报 `找不到 lark-cli` | `Test-Path` 配置里的 `feishu.cli_path`；直接执行该 exe `base record-list` | 把 `lark-cli.exe` 复制到状态目录并在 `config.json` 配置绝对路径；不要只依赖登录自启进程的 PATH |
| 同步飞书报 `parse temporary token from Authorization fail` | `cli-env.json` 的临时 token 是否过期 | 当前 token 是一次性快照，不能长期复用；生产改用持久飞书应用凭据/OAuth profile，过渡验证可在有 Agent 会话时直接跑同步 |
| 登录页提示“登录已失效”但密码已填 | 服务端 `/api/login` 返回 401 的 `message` | 密码错误现在会提示“密码不正确”；若页面仍是旧提示，强制刷新以加载新版 `app.js` |
| 迁移组提示“飞书当前视图中已找不到此记录” | DB 中组 ID 是否仍是 `grp_...` | 同步会按周/摄影师/图名/提交日/附件数精确接入并改键为 record_id；改键后重复同步不再新建组 |
| 登录后立刻失效 | 是否另一台设备登录 | 单会话设计；在新设备登录会撤销旧会话 |
| 草稿失败 | CDP `/json/version`、创作者登录态 | 恢复 Chrome/CDP；重新生成草稿 |
| 发布结果不确定 | 后台是否已有笔记 | 人工核对；不要重复点击 |
| 123 上传失败 | `upload_tasks.error`、云盘登录/目录 | 修复后重试；失败记录不会消失 |
| 数据库锁/无法删除 | 是否有残留连接 | 停进程后检查 `-wal`/`-shm`；用备份恢复 |
| 周任务未跑 | 服务是否在周一 20:00 运行 | 重启后调度器会补跑；检查设置时间 |
| 迁移冲突 | `migration-plan.json` | 人工归组/改名；不要覆盖旧文件 |

## 10. 生产发布门禁

上线前至少完成：

1. 代码级测试全部通过（见 `TEST_REPORT.md`）。
2. 测试账号完成草稿 + 预览 + 发布回读。
3. 测试 123 目录完成上传、重复执行、跨周清理。
4. 公网单会话验收。
5. 数据库/配置/周目录备份可恢复。
6. 正式账号发布另行取得明确授权。

## 11. 本次生产切换记录（2026-11-17）

1. 备份旧 `niguang365\config.json` 到 `D:\逆光365图片下载\.niguang365\backups\config-before-v2-20261117-011530.json`。
2. 将配置转换为 v2：`data_root=D:\逆光365图片下载`、`archive_parent=<data_root>\已归档`、`state_root=<data_root>\.niguang365`、`db_path=<state_root>\state\niguang365.sqlite3`、`fixed_tags=['#电子科技大学','#电子科技大学逆光摄影']`、`pan123.root_path=我的文件/逆光摄影工作室/逆光日记365/2026-2027`。
3. 在 8599 端口以 v2 配置启动并验证 `/api/health` 返回 `{"ok":true,"name":"逆光365"}`；随后执行迁移干跑：2 个日目录、4 张照片、2 个组、2 个入选组、0 冲突。
4. 执行 `--apply` 迁移：写入 2 组、复制 4 图；迁移备份在 `D:\逆光365图片下载\.niguang365\backups\migration-20261117-011616`，迁移前数据库为 `before-migration.sqlite3`。
5. 生成周目录 `已归档\2026\20260914-0920`、`已归档\2026\20261116-1122`；旧 `已归档\2026\20260915` 与旧 `收件箱` 保留不删。
6. 停止旧 `niguang365.web` 临时进程，启动 v2 到 8501；公网 `/api/health` 从旧 `{"ok":true,"service":"niguang365-web"}` 变为 `{"ok":true,"name":"逆光365"}`，公网首页返回 200。
7. 用临时会话完成 `/api/dashboard`、`/api/weeks`、`/api/settings`、`/api/accounts` smoke；未使用用户密码。
8. 迁移后真实周状态：`20261116=collecting`、`needs_sync=true`、1 组；`20260914=upload_blocked`、`needs_sync=true`、1 组（旧数据已入选但无新发布记录）。
9. 摄影师账号 CSV 只有占位行 `陈权,请填小红书号`，已清空数据库映射为 0；需补真实小红书号后重新导入，文案才会出现 `姓名@小红书号`。
10. 当前 Web/Chrome 由 HKCU Run 登录自启项管理，已实测停掉临时进程后可由 `start-stack.vbs` 在本地恢复 8501 与 9222；公网健康检查通过。
11. 飞书同步曾报 `找不到 lark-cli`：原因是自启进程/Anaconda 手动进程的 PATH 里没有 Doubao 沙箱内的 `lark-cli.exe`，且 v2 的 `FeishuConfig` 原先未把 `cli_path` 传给 Web 适配器。已把 `lark-cli.exe` 固定到 `D:\逆光365图片下载\.niguang365\tools\lark-cli.exe`，在 `config.json` 写入 `feishu.cli_path`，并修复 `FeishuConfig -> FeishuAdapter -> OfficialFeishuBase` 的传递；真实只读验证返回 3 组、0 跳过。

尚未完成的生产验收：真实共享密码登录、真实飞书同步、小红书草稿/发布、123 上传、公网单会话。不要把本地 smoke 当成这些业务验收。
