# 逆光365 架构说明

版本：v2（业务层重建）
最后验证：2026-11-17
适用代码：`niguang365/`（Windows 本地部署）

## 1. 系统目标

逆光365 是一个**单主编、移动优先的本地 Web 工作台**：

1. 只读同步飞书投稿表当周记录。
2. 一条飞书记录 = 一个投稿组，整组一起入选。
3. 按组生成可编辑的小红书笔记，服务端通过 Chrome/CDP 创建独立草稿。
4. 主编在小红书后台预览后，由 Web 明确确认，服务端复核并点击原生发布。
5. 周一 20:00 收束上一周、未入选组改名、整周目录上传 123 云盘；失败持久留痕并可重试。

非目标：不读取点赞/播放/收藏/评论等互动数据；不在云端保存原图；不自动替主编决定入选；不提供多用户权限系统。

## 2. 分层与模块

```
┌──────────────────────────────────────────────────────┐
│ webui/index.html + style.css + app.js（移动优先）      │
└──────────────────────────┬───────────────────────────┘
                           │ JSON / Cookie Session
┌──────────────────────────▼───────────────────────────┐
│ webapp.py（FastAPI 路由、认证、受控媒体流、错误映射）   │
└──────────────────────────┬───────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────┐
│ core/service.py（业务编排：同步、选组、组稿、发布、收束）│
└───────┬──────────────┬───────────────┬───────────────┘
        │              │               │
┌───────▼──────┐ ┌─────▼───────┐ ┌─────▼──────────────┐
│ core/repo.py │ │ core/domain │ │ core/scheduler.py  │
│ SQLite 访问  │ │ 规则/命名   │ │ 周一 20:00 补跑    │
└───────┬──────┘ └─────────────┘ └────────────────────┘
        │
┌───────▼──────────────────────────────────────────────┐
│ core/db.py（SQLite schema、事务、WAL、审计、会话）     │
└──────────────────────────────────────────────────────┘

外部适配器：
- core/feishu_adapter.py  → 飞书只读记录/附件
- core/xhs_adapter.py     → 小红书 CDP 草稿/预览/发布
- core/cloud_adapter.py   → 123 云盘上传/核验/受控清理
- core/accounts.py        → 摄影师 CSV 映射
```

### 2.1 关键文件

| 文件 | 职责 | 不应该做什么 |
|---|---|---|
| `core/config.py` | 配置模型、默认口径、路径展开 | 不写业务状态 |
| `core/db.py` | 表结构、WAL、事务、审计、单会话 | 不决定入选 |
| `core/domain.py` | 周计算、day 编号、文件名、文案、标签校验 | 不访问网络 |
| `core/repo.py` | 周/组/照片/笔记/草稿/话题/上传任务/会话的数据访问 | 不移动文件 |
| `core/service.py` | 业务编排、文件改名、外部适配器调用、设置 | 不直接解析 HTML |
| `core/auth.py` | 共享密码、PBKDF2、服务端单会话 | 不做多用户权限 |
| `core/scheduler.py` | 周一 20:00 收束/上传补跑 | 不改系统计划任务 |
| `webapp.py` | FastAPI 入口、认证守卫、API、媒体流 | 不复制业务规则 |
| `webui/` | 登录、工作台、笔记、归档、设置 | 不直接访问文件系统 |

## 3. 数据契约

SQLite 是**唯一业务事实源**。文件系统是数据库状态的物化结果；页面显示的入选状态、改名结果、上传失败都从数据库读取。

### 3.1 表

| 表 | 作用 | 关键字段 |
|---|---|---|
| `settings` | 运行期可改配置 | `key/value/updated_at` |
| `accounts` | 摄影师→小红书号映射 | `name/handle/source` |
| `weeks` | 周目录与状态 | `id/start_date/end_date/directory/status/needs_sync` |
| `groups` | 一条飞书记录=一组 | `week_id/photographer/title/quote/submitted_at/status/selected_date/note_id/issue` |
| `photos` | 组内图片 | `group_id/token/sort_order/path/bytes/missing_remote/remote_name/previous_remote_name` |
| `notes` | 小红书笔记草稿 | `title/day_number/content/publish_date/status/account/fingerprint/note_url` |
| `note_groups` | 笔记与组的来源关系和页码 | `note_id/group_id/page_start/page_end` |
| `drafts` | 平台草稿定位与内容指纹 | `note_id/locator/fingerprint/image_count/status` |
| `topics` | 实时话题候选与采用状态 | `name/views_raw/views_number/fetched_at/adopted/sort_order` |
| `upload_tasks` | 周上传任务 | `week_id/root_path/status/error/summary/attempt` |
| `upload_items` | 任务内逐文件结果 | `photo_id/filename/remote_name/status/error` |
| `sessions` | 单活动会话 | `token_hash/expires_at/revoked_at` |
| `audit_logs` | 审计 | `action/target/detail` |
| `sync_runs` | 同步报告 | `week_id/status/report` |

### 3.2 状态机

**周状态**：`collecting → closed → upload_blocked | upload_failed | uploaded → cleanup_pending`

- `collecting`：收集/入选阶段。
- `closed`：已收束，未入选组已改名。
- `upload_blocked`：存在已入选未发布组，上传被显式阻断。
- `upload_failed`：上传失败，持久保留错误。
- `uploaded`：上传并核验成功。
- `cleanup_pending`：新版本已上传，但旧远端文件未确认清理。

**组状态**：`collected → selected → published`，或 `collected/selected → unselected`

- `collected`：已同步，尚未入选。
- `selected`：整组入选，文件带 `（MMDD入选）`。
- `published`：已通过 Web 发布并保存笔记记录。
- `unselected`：本周不发布，收束后文件带 `（未入选）`。

**上传任务**：`queued → running → uploaded | failed | uploaded_with_cleanup_pending`

## 4. 关键业务规则

### 4.1 周与 day

- 周：ISO 周（周一至周日），目录 `YYYY/YYYYMMDD-MMDD`。
- 开发默认：`D:\逆光365图片下载\已归档\YYYY\YYYYMMDD-MMDD`；部署时在 `config.json` 配置工作室路径。
- day 基准：`2022-11-15 = day0001`；`2026-11-13 = day1460`。

### 4.2 组与命名

- 一条飞书记录 = 一个组；组内全部图片一起入选，不能只选单图。
- 同一摄影师同日多次提交仍是多个组。
- 合并只体现在同一篇小红书笔记，本地目录不合并。
- 文件名：`YYYYMMDD_姓名_图名[序号]（MMDD入选）.扩展名`
  - 单图无序号；多图从 1 开始。
  - 未入选：`（未入选）`，不写入选日期。
  - 取消入选：移除后缀。
  - 已入选未发布：保留 `（MMDD入选）`。
- 跨周未入选组可以再次入选，文件仍留在原周目录，并标记 `needs_sync`。

### 4.3 文案

- 单组：标题 `《图名》`，正文含 day、署名、可选图说、实际发布日、标签。
- 合并：标题由主编填写，按页范围列出各组作品名/署名/图说；图片总数守恒。
- 标签在最后一个非空行；固定标签默认 `#电子科技大学`、`#电子科技大学逆光摄影`。
- 图说原文保留，允许 emoji 和换行；系统只校验标签末行，不做内容审核。
- 账号按姓名精确匹配 CSV；缺映射只写姓名，不阻断。
- 日期矛盾只提示，不拦截。

### 4.4 草稿与发布

1. Web 选择组、生成笔记、刷新实时话题。
2. `POST /api/notes/{id}/draft` 由服务端 CDP 在小红书创作者后台创建**独立草稿**，保存 locator/指纹。
3. 主编通过 Web 的“打开后台”跳转到已登录的创作者页面，人工预览。
4. `POST /api/notes/{id}/publish` 需 `confirmed=true`；服务端重新读取当前草稿，复核标题/正文/图片数/话题，点击原生发布。
5. 结果不确定时返回 `needs_review`，不写成功、不自动重试；组保持已入选。

### 4.5 周收束与 123

- 周收束先检查 `selected_unpublished`；存在则阻断并提示，不静默改名。
- 已发布组跳过；未入选组统一改名。
- 周一 20:00 由 `scheduler.py` 收束上一周并排队上传；服务恢复后补跑。
- 上传前再次检查 `selected_unpublished`；上传后回读文件数/大小/结构。
- 跨周改名：先把新文件上传核验，再只清理明确对应的旧远端文件；清理失败置 `cleanup_pending`。
- 失败持久在 `upload_tasks`，刷新不会消失。

## 5. 认证与安全边界

- 共享密码，PBKDF2-SHA256 240,000 轮；`HttpOnly` Cookie；默认 7 天绝对有效期。
- 服务端只保留一个活动会话；新登录撤销旧会话；旧设备下一次请求返回 401。
- 不设置密码错误次数锁定（按需求）。
- 所有业务 API 默认要求登录；`/api/health` 和静态登录页例外。
- 媒体流只允许数据库登记的照片路径，拒绝目录穿越。
- 私密配置：`niguang365/config.json`、`niguang365/web.local.json` 被 Git 忽略。

## 6. 架构决策记录（ADR）

### ADR-001：用 SQLite 取代 JSON/文件名作为事实源

- 背景：旧系统以 `.niguang365-selection.json`、`.niguang365-published.json` 和文件名共同判定状态，刷新/重启后容易不一致。
- 决策：SQLite 唯一事实源；文件系统是物化结果。预览 JSON 只作为导出兼容，不再参与判定。
- 后果：需要迁移脚本；换来状态可查询、可恢复、可审计。

### ADR-002：Web 取代 CLI 自动发布

- 背景：旧 CLI 可直接自动发布，无法满足“主编最终确认”和防重。
- 决策：`python -m niguang365` 默认启动 Web；旧 `cli.py` 仅保留维护/诊断能力，标记废弃。
- 后果：主编必须通过 Web 确认发布；自动化测试用 fake adapter。

### ADR-003：平台动作放在适配器，业务状态留在服务层

- 背景：小红书/123 页面结构高频变化，不能把 DOM 逻辑散落到业务层。
- 决策：`xhs_adapter.py`、`cloud_adapter.py`、`feishu_adapter.py` 只负责平台动作；`service.py` 只做编排和状态转换。
- 后果：平台变化时替换适配器；业务测试可注入 fake。

### ADR-004：失败持久化，不伪造成功

- 背景：发布/上传结果不确定时，自动重试可能造成重复笔记或重复文件。
- 决策：上传失败写任务表；发布不确定返回 `needs_review`；不建立独立“待确认发布”状态，只提示人工核对。
- 后果：主编需要手动处理失败任务；换来不重复发布、不重复上传。

### ADR-005：移动优先，不引入重型前端框架

- 背景：手机是主编主要入口，需要弱网/小屏可用。
- 决策：原生 HTML/CSS/JS，单页四区（工作台/笔记/归档/设置），无构建步骤。
- 后果：没有 React/Vue 生态；换来低依赖、易部署、可直接由 FastAPI 静态托管。

## 7. 旧链路退役

| 旧对象 | 处置 |
|---|---|
| `cli.py` 自动发布 | 标记废弃；`python -m niguang365` 不再进入旧 CLI |
| `web.py` | 保留为历史兼容/测试参考，不作为主入口 |
| `feishu.py` / `collect.py` / `compose.py` / `naming.py` | 旧链路模块，不再被新 `service.py` 调用；仅迁移/兼容测试使用 |
| `static/` 旧前端 | 保留历史文件，新入口使用 `webui/` |
| `.niguang365-selection.json` / `.niguang365-published.json` | 不再作为事实源；迁移工具只读导入 |
| `收件箱/YYYYMMDD` | 旧下载落点；新链路按周目录 |

## 8. 已知限制

- 真实飞书、小红书草稿/发布、123 上传、公网单会话尚未在本轮做真实平台验收；见 `TEST_REPORT.md`。
- 平台标题/图片限制以提示为主，真实阈值待 T-002 验证。
- 旧数据迁移为“复制导入”，不删除旧文件；冲突交人工。
- Windows 自启动、电源策略、Cloudflare Tunnel 服务只生成清单，不在开发机执行。
