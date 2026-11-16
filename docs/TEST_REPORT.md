# 测试报告（代码级）

日期：2026-11-17
范围：仅逻辑、代码、接口与本地进程验证。**未执行真实飞书同步、小红书草稿/发布、123 上传、公网单会话验收**（按本轮要求暂缓）。

## 1. 测试环境

| 项目 | 值 |
|---|---|
| OS | Windows（本地开发机） |
| Python | `C:\Users\chenquan\.workbuddy-ai\binaries\python\envs\xhs\Scripts\python.exe`（3.13.14） |
| Web | FastAPI TestClient + 本地 uvicorn 启动检查 |
| 外部平台 | 全部使用 fake/stub；未连接真实平台 |
| 数据 | 临时 SQLite/临时周目录；旧收件箱只做干跑 |

## 2. 全量回归

命令：

```powershell
& "C:\Users\chenquan\.workbuddy-ai\binaries\python\envs\xhs\Scripts\python.exe" `
  -m unittest discover -s niguang365/tests -v
```

结果：

```text
Ran 91 tests in 14.429s
OK
```

覆盖文件：

- `test_core.py`：旧命名/文案/发布前检查（28 个）
- `test_feishu.py`：飞书字段/附件/去重（19 个）
- `test_pan123.py`：123 目录清单/上传结果（8 个）
- `test_v2.py`：新业务层（19 个）
- `test_web.py`：旧 Web 受控发布/媒体流（13 个）
- `test_migration.py`：新迁移工具（2 个）
- 合计 91 个，全部通过。

## 3. v2 新增覆盖

| 用例组 | 覆盖点 |
|---|---|
| `DomainTests` | 周计算、day 编号、单图/多图命名、冲突拒绝、单组/合并文案、标签末行 |
| `ServiceTests` | 同步建组、整组入选/取消、日期矛盾只提示、组同步、跨周未入选再入选、草稿、发布、周收束阻断、上传失败持久化、调度补跑、幂等收束、账号 CSV 导入与占位行过滤 |
| `AuthTests` | 单会话、改密接管、错误不锁定 |
| `WebApiTests` | 登录守卫、Cookie 会话、选组/组稿/设置 API、发布必须显式确认 |

## 4. 其他代码级验证

| 检查 | 命令 | 结果 |
|---|---|---|
| 编译 | `python -m compileall -q niguang365` | 通过 |
| 模块导入 | `python -c "import niguang365.webapp"` | 通过 |
| 前端 JS 语法 | `node --check niguang365/webui/app.js` | 通过 |
| Web 启动 | `python -m niguang365.webapp --port 8601 --no-scheduler` | 启动成功 |
| 健康接口 | `GET http://127.0.0.1:8601/api/health` | 200 `{"ok":true,"name":"逆光365"}` |
| 首页 | `GET http://127.0.0.1:8601/` | 200，返回登录页 |
| 迁移干跑 | `python -m niguang365.migrate_legacy --config niguang365\config.json --json` | 识别 2 个日目录、4 张照片、2 组；无冲突；未写文件/数据库 |

## 5. 已修复的代码级问题（本轮）

1. `Database.initialize()` 只提交不关闭连接 → 改用 `closing`，Windows 临时 SQLite 可删除。
2. `day` 基准与任务书样本不一致 → 校正为 `2022-11-15`，样本 `2026-11-13=day1460`。
3. 实时话题未过滤固定标签 → `refresh_topics()` 过滤 `#电子科技大学` 等固定标签。
4. 已收束周重复收束可能清除远端名 → 目标名未变化则跳过。
5. `queue_upload()` 对 `closed` 周不再收束 → 改为始终调用收束，靠“未变化跳过”保持幂等。
6. 显式取消入选后仍残留入选日期 → `_plan()` 使用 `_UNSET` sentinel，`selected_date=None` 可清除。
7. 旧数据迁移新增：干跑、备份、逐组冲突跳过、同大小跳过、不删除旧文件。

## 6. 明确未执行/未覆盖

| 项目 | 状态 |
|---|---|
| 真实飞书只读同步 | 未执行；等真实表格/凭据验收 |
| 小红书真实草稿创建/定位 | 未执行；仅 fake |
| 小红书真实发布、成功页回读 | 未执行；测试账号发布另行授权 |
| 123 真实上传/重名/跨周清理 | 未执行；仅 fake/清单测试 |
| 公网单会话 | 未执行；仅 TestClient/本机启动 |
| CDP 断开、页面结构变化、上传中断、服务重启 | 未做完整故障注入；部分由 fake 覆盖 |
| 浏览器 360/768/1440 视觉验收 | 未做截图级验收；仅 JS 语法/API |
| 任务书 T-002/T-051 | 明确缓期，见追踪表 |

## 7. 结论

- 代码级逻辑、命名、状态机、认证、API、迁移、启动均通过。
- 具备“可部署”的代码条件，但**不具备声称已通过真实平台验收的条件**。
- 上线前必须按 `DEPLOYMENT.md` 第 10 节完成真实测试账号/测试目录/公网验收。
