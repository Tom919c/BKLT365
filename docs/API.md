# Web API 说明

版本：v2
入口：`/api/*`，同源 Cookie 认证
响应约定：成功为 JSON；业务错误 `{"ok": false, "message": "..."}`

## 1. 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 无需登录；返回 `{"ok":true,"name":"逆光365"}` |
| GET | `/api/session` | 查询当前 Cookie 会话 |
| POST | `/api/login` | `{"password":"..."}`；成功设置 HttpOnly `niguang365_session` |
| POST | `/api/logout` | 撤销当前会话并清 Cookie |
| POST | `/api/settings/password` | `{"current":"...","new":"..."}`；改密后接管当前会话 |

除 `/api/health`、`/api/session`、`/api/login` 外，全部要求有效会话。会话失效返回 401，前端提示“如已在另一台设备登录，请重新登录”。

## 2. 工作台与同步

| 方法 | 路径 | 请求 | 说明 |
|---|---|---|---|
| GET | `/api/dashboard?week=YYYYMMDD` | 可选周起始 | 返回周、组、笔记、统计、告警 |
| GET | `/api/weeks` | — | 历史周列表 |
| POST | `/api/sync` | `{"week":"YYYYMMDD"}` 或 `{}` | 只读同步；幂等；返回同步报告 |
| POST | `/api/groups/{group_id}/select` | `{"publish_date":"YYYY-MM-DD"}` | 整组入选并立即改名 |
| POST | `/api/groups/{group_id}/unselect` | — | 整组取消入选 |
| PATCH | `/api/groups/{group_id}` | `{"title":"...","quote":"..."}` | 组级图名/图说 |
| PUT | `/api/groups/{group_id}/order` | `{"photo_ids":["..."]}` | 组内图片排序 |

`select` 的 `publish_date` 是入选/实际发布日；与投稿日不同只提示，不阻断。

## 3. 笔记与话题

| 方法 | 路径 | 请求 | 说明 |
|---|---|---|---|
| GET | `/api/notes` | — | 笔记列表（含状态、图片数、组来源） |
| POST | `/api/notes` | `{"group_ids":[...],"title":null,"publish_date":"...","day":null,"dynamic_topics":[]}` | 单组/合并组稿 |
| GET | `/api/notes/{note_id}` | — | 笔记详情、正文、图片、话题 |
| PATCH | `/api/notes/{note_id}` | `{"title":"...","content":"...","day":"..."}` | 编辑文案；标签末行校验 |
| POST | `/api/notes/{note_id}/topics/refresh` | — | 实时读取话题浏览量；失败明确返回 |
| PUT | `/api/notes/{note_id}/topics` | `{"names":["#..."]}` | 保存采用话题（固定标签不能再写） |

## 4. 草稿与发布

| 方法 | 路径 | 请求 | 说明 |
|---|---|---|---|
| POST | `/api/notes/{note_id}/draft` | — | 服务端 CDP 创建独立草稿，保存 locator/指纹 |
| POST | `/api/notes/{note_id}/open-native` | — | 打开已登录创作者后台，供人工预览 |
| POST | `/api/notes/{note_id}/publish` | `{"confirmed":true}` | 复核当前草稿并点击原生发布 |

发布约束：

- `confirmed` 必须显式为 `true`。
- 服务端复核标题、正文、图片数、话题；不匹配拒绝发布。
- 已发布笔记不可重复发布。
- 结果不确定返回 `needs_review`（HTTP 200 或业务错误，视实现），不写成功、不自动重试。

## 5. 周收束与 123

| 方法 | 路径 | 请求 | 说明 |
|---|---|---|---|
| POST | `/api/weeks/{week_id}/close` | — | 收束；有已入选未发布时阻断 |
| POST | `/api/weeks/{week_id}/upload` | `{"allow_current":false}` | 排队上传；`allow_current` 允许当前周（谨慎） |
| GET | `/api/upload-tasks?week=YYYYMMDD` | — | 历史任务与持久失败 |
| POST | `/api/upload-tasks/{task_id}/retry` | — | 重试失败任务 |

上传先再次检查 `selected_unpublished`；失败写任务表，页面刷新不会清除。

## 6. 设置、账号与审计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/settings` | 业务设置、运行路径、自启清单 |
| PUT | `/api/settings` | 可更新：`pan123_root`、上传时间、`auto_start`、`fixed_tags`、话题数、发布模式、正式授权、账号备注 |
| GET | `/api/settings/autostart` | 六项部署清单 |
| GET | `/api/accounts` | 摄影师映射 |
| POST | `/api/accounts/import` | 从配置的 CSV 导入；导入前备份 |
| GET | `/api/audit` | 最近 30 条审计 |
| GET | `/api/health/detail` | 数据库 + CDP 健康；不执行发布/上传 |

`PUT /api/settings` 的 `fixed_tags` 可传字符串（逗号分隔）或数组；至少 1 个、最多 10 个，自动补 `#`。

## 7. 媒体

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/media/photo/{photo_id}` | 只能读取数据库登记且位于 `archive_parent` / `staging_root` 内的原图；拒绝目录穿越 |

## 8. 前端静态

- `GET /`：`niguang365/webui/index.html`
- `GET /style.css`、`/app.js`：同目录静态资源
- 不暴露 FastAPI 自带 `/docs`、`/redoc`、`/openapi.json`（`docs_url=None`）。
