# 配置、部署与运维

版本：v2
最后验证：2026-11-17
适用：Windows 10/11 工作室电脑（本地部署）

> 当前开发电脑只做代码级验证。本文中的电源策略、启动项、计划任务、Cloudflare Tunnel 服务、防火墙规则均为**部署清单**，不得在开发机擅自执行。

## 1. 运行前提

| 组件 | 要求 |
|---|---|
| Windows | 10/11，建议常开、接通电源 |
| Python | 3.11+；推荐 3.13 虚拟环境 |
| 依赖 | `niguang365/requirements.txt` |
| Chrome | 独立 `XiaohongshuProfile`，固定 CDP 端口 9222 |
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

`config.example.json` 只含占位符；真实凭据只写入本机 `config.json`。`web.local.json` 由 `--init-auth` 生成，不要手工伪造。

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
  --user-data-dir="$env:USERPROFILE\XiaohongshuProfile" `
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

部署机（不是当前开发机）操作：

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

## 7. 开机自启与电源（部署机执行）

Web 与项目 Chrome 必须由**同一个总开关**管理。建议用任务计划程序，在“用户登录时”启动：

- Web：工作目录 `D:\Code\VSCode\BKLT365`，命令 `python -m niguang365.webapp --host 127.0.0.1 --port 8501`。
- Chrome：上面的独立 profile + CDP 命令。
- 电源：永不睡眠、合盖不睡眠、接通电源。
- 防火墙：只允许本机/局域网访问 8501；公网仅经 Tunnel。
- 执行前记录原设置，执行后按设置页清单逐项验证；不要静默修改。

> 当前开发电脑不得修改电源策略、启动项、计划任务、Cloudflare Tunnel 服务。

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
