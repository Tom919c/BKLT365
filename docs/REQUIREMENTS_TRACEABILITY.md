# 需求追踪表（T-001—T-052）

日期：2026-11-17
状态定义：

- **完成**：实现、代码级测试、文档均有证据。
- **部分**：代码/测试完成，但真实平台或视觉验收未做。
- **缓期**：按本轮要求不做实际平台测试，已记录在测试报告/部署门禁。
- **不做/保留**：用户明确暂缓或明确保留。

## 1. 总表

| 任务 | 状态 | 实现/证据 | 未完成项 |
|---|---|---|---|
| T-001 业务样本学习与需求追踪 | 部分 | 已按任务书/现状基线/真实笔记与周目录样本建立业务口径；本文件为追踪表 | 未形成逐篇 20 篇笔记的独立清单；本轮未重新读取平台数据 |
| T-002 外部平台能力验证 | 缓期 | 代码按“限制只提示”实现；测试报告列明未验证 | 标题/图片/多草稿/话题浏览量/123 真实页面验证 |
| T-003 目标架构与迁移方案冻结 | 完成 | `docs/ARCHITECTURE.md`、`docs/MIGRATION.md`、ADR-001—005 | — |
| T-010 SQLite 与配置基座 | 完成 | `core/db.py`、`core/config.py`、`test_v2`、`docs/DEPLOYMENT.md` | — |
| T-011 周目录与命名引擎 | 完成 | `core/domain.py`、`core/service.py`、`test_v2.DomainTests` | — |
| T-012 飞书当周同步 | 部分 | `core/feishu_adapter.py`、`test_v2` fake 同步；只读、幂等、图说保留 | 真实飞书只读同步 |
| T-013 摄影师账号映射 | 完成 | `core/accounts.py`、`test_core`/`test_v2` | — |
| T-014 共享登录与单会话 | 完成 | `core/auth.py`、`test_v2.AuthTests`、`test_web` | — |
| T-020 本周工作台与按组选择 | 部分 | `webapp.py`、`webui`、`test_v2` API/服务测试 | 手机浏览器视觉/交互验收 |
| T-021 文案编辑与合并笔记生成 | 完成 | `core/domain.py`、`test_v2.DomainTests` | — |
| T-022 实时动态话题 | 部分 | `core/xhs_adapter.py`、`test_v2` 过滤固定标签；失败不静默缓存 | 真实话题浏览量 DOM/接口验证 |
| T-023 服务端 CDP 草稿与原生预览 | 部分 | `core/xhs_adapter.py` + fake；独立草稿、指纹、定位 | 真实草稿箱/定位/回读 |
| T-024 Web 确认发布与防重 | 部分 | `test_v2`/`test_web` 确认、复核、不确定不写成功 | 测试账号真实发布 |
| T-030 周收束与定时任务 | 完成 | `service.close_week`、`scheduler.py`、`test_v2` 补跑/幂等 | 真实 Windows 常开环境长跑 |
| T-031 123 上传中心与跨周同步 | 部分 | `pan123.py`、`cloud_adapter.py`、`test_pan123`、fake 上传 | 真实 123 上传/重名/跨周清理 |
| T-032 软件设置与开机自启总开关 | 完成 | `settings_view/update_settings`、`webui` 设置页、六项清单；未改本机系统 | 工作室实际注册启动项（部署时） |
| T-040 移动端优先界面重构 | 部分 | `webui/` 360/768/1440 响应式、状态提示；`docs/DESIGN_SYSTEM.md` | 浏览器截图级视觉验收 |
| T-041 开发者、部署、用户文档 | 完成 | `docs/ARCHITECTURE/DEPLOYMENT/USER_GUIDE/API/TEST_REPORT/MIGRATION`；README 更新 | — |
| T-042 旧链路退役与数据迁移 | 完成 | `migrate_legacy.py` 干跑/备份/冲突；`__main__` 改 Web；CLI 保留诊断 | 真实旧数据全量迁移需部署后执行 |
| T-050 自动化测试与故障注入 | 部分 | 94 项测试；fake 发布/上传/草稿 | CDP 断开、页面结构变化、上传中断、服务重启完整注入 |
| T-051 端到端验收与发布门禁 | 缓期 | 部署文档第 10 节门禁；测试报告明确未执行 | 真实闭环全部场景 |
| T-052 交付与回滚 | 部分 | 配置模板、文档、迁移备份/回滚、测试报告 | 干净部署机实测、正式发布包 |

## 2. 业务规则映射

| 规则 | 实现位置 | 测试/文档 |
|---|---|---|
| 一条记录=一组，整组入选 | `feishu_adapter.py`、`service.select_group` | `test_sync_creates...`、`test_select...` |
| 周目录 `YYYY/YYYYMMDD-MMDD` | `domain.week_for/path` | `test_week_and_day_number` |
| 文件名 `YYYYMMDD_姓名_图名[序号]（MMDD入选）` | `domain.build_photo_name` | `test_build_photo_name_multi_and_suffix` |
| 多图从 1 开始，单图无序号 | `domain.build_photo_name` | 同上 |
| 未入选 `（未入选）` | `domain.selection_suffix` | `test_unselect_after_close...` |
| 合并仅同一笔记，不合并目录 | `service.create_note`、`note_groups` | `test_merge_two_groups_same_date` |
| 图说可选、emoji/换行保留 | `feishu_adapter.py`、`domain.compose_*` | `test_sync...`、`test_compose...` |
| 标签最后一行 | `domain.validate_tags_line` | `test_tags_validation` |
| 账号姓名精确匹配 | `accounts.py` | 旧测试 |
| 实时话题失败不静默缓存 | `service.refresh_topics` | `test_create_note...`（fake 失败） |
| 草稿独立、不覆盖 | `xhs_adapter`、`drafts` | `test_create_note_topics_draft...` |
| 发布需显式确认/防重 | `webapp`、`service.publish_note` | `test_web_publish_requires...` |
| 周收束阻断已入选未发布 | `service.close_week` | `test_close_week_blocks...` |
| 上传失败持久 | `upload_tasks`、`service.process_upload` | `test_upload_after_close...` |
| 单会话 | `auth.py`、`sessions` | `test_single_session...` |
| 受控媒体流 | `webapp.photo` | `test_media_serves_original...` |

## 3. 用户明确保留/暂缓

| 项 | 状态 |
|---|---|
| 共享密码、单会话 | 保留并实现 |
| 人工最终选图/发布 | 保留 |
| Windows 常开 + Cloudflare Tunnel | 保留；开发机不改服务 |
| 原图不压缩 | 保留 |
| 飞书只读 | 保留 |
| 主编用户手册 | 保留并交付 |
| Cloudflare Access、按人授权、安全加固、三端对账、图片内容哈希、独立待确认状态、统计仪表盘、定时同步/发布、AI 自动选图、微信群通知、CLI 自动发布 | 不做/暂缓 |

## 4. 结论

代码级任务已闭环到可部署状态；真实平台验收和完整视觉/故障注入仍为上线门禁。不得把本追踪表的“部分/缓期”当作已完成。
