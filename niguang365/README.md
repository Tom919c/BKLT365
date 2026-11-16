# niguang365 包说明

本包已切换为 **v2 Web 工作台**。正式入口：

```powershell
python -m niguang365            # 等价于 niguang365.webapp
python -m niguang365.webapp --host 127.0.0.1 --port 8501
```

完整部署、配置、主编操作与 API 说明见仓库根目录的：

- [`../docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md)
- [`../docs/USER_GUIDE.md`](../docs/USER_GUIDE.md)
- [`../docs/API.md`](../docs/API.md)
- [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md)

## v2 目录

| 路径 | 说明 |
|---|---|
| `core/` | 配置、SQLite、领域规则、仓储、飞书/小红书/123 适配器、业务服务、认证、调度 |
| `webui/` | 移动优先前端（登录/工作台/笔记/归档/设置） |
| `webapp.py` | FastAPI 主入口 |
| `migrate_legacy.py` | 旧收件箱只读迁移（默认干跑） |
| `tests/` | 90 项代码级测试 |

## 旧链路

以下文件保留用于历史兼容、迁移和诊断，**不是主编主流程**：

- `cli.py`：旧 CLI；自动发布已废弃，只能显式 `python -m niguang365.cli` 调用。
- `web.py`、`static/`：旧 Web 与旧前端。
- `feishu.py`、`collect.py`、`compose.py`、`naming.py`：旧日目录/单图勾选链路。

新链路不再使用 `收件箱/YYYYMMDD` 作为目标落点，不再以 `.niguang365-selection.json` / `.niguang365-published.json` 作为事实源。

## 测试

```powershell
python -m unittest discover -s niguang365/tests -v
python -m compileall -q niguang365
```

最近结果：`Ran 90 tests ... OK`。未执行真实平台验收，详见 [`../docs/TEST_REPORT.md`](../docs/TEST_REPORT.md)。
