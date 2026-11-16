# 逆光365 文档索引

本目录面向三类读者：

- **轮值主编**：先读 [主编用户手册](USER_GUIDE.md)。
- **开发者/维护者**：读 [架构说明](ARCHITECTURE.md)、[配置与部署](DEPLOYMENT.md)、[API](API.md)、[测试报告](TEST_REPORT.md)。
- **迁移与回滚**：读 [迁移与回滚](MIGRATION.md)。

## 文档清单

| 文档 | 用途 |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 目标架构、模块边界、状态机、数据模型、ADR |
| [DEPLOYMENT.md](DEPLOYMENT.md) | 开发环境、Windows 工作室部署、Chrome/CDP、123、Tunnel、备份与故障恢复 |
| [USER_GUIDE.md](USER_GUIDE.md) | 主编从同步到发布、收束、上传、跨周入选的完整操作手册 |
| [API.md](API.md) | Web API 路由、权限、请求/响应约定 |
| [MIGRATION.md](MIGRATION.md) | 旧收件箱/旧 JSON 迁移、干跑、冲突处理、回滚 |
| [TEST_REPORT.md](TEST_REPORT.md) | 本轮代码级测试证据、覆盖范围、未执行的真实平台验收 |
| [REQUIREMENTS_TRACEABILITY.md](REQUIREMENTS_TRACEABILITY.md) | 任务书 T-001—T-052 与实现、测试、文档的追踪 |
| [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) | 移动优先界面、设计资源调研记录、状态与可访问性约定 |

## 文档维护规则

- 业务规则变化时，同时更新 `ARCHITECTURE.md` 的状态机/数据契约、`USER_GUIDE.md` 的操作步骤、`REQUIREMENTS_TRACEABILITY.md` 和对应测试。
- 不把真实密码、Cookie、飞书凭据、123 登录态、本机私密路径写入本目录。
- 真实平台验收证据单独存放在受控位置；本目录只记录验证结论与限制。
