# BKLT365 / 逆光365

电子科技大学逆光摄影社团「365 日更」项目的投稿收集、组图编排、小红书草稿/发布与周归档工作台。

当前主链路：**移动优先 Web 工作台**（FastAPI + SQLite + 本地 Chrome/CDP），CLI 自动发布仅保留维护用途。

## 快速开始

```powershell
git clone --recurse-submodules https://github.com/Tom919c/BKLT365.git
cd BKLT365
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r niguang365\requirements.txt
Copy-Item niguang365\config.example.json niguang365\config.json
# 按 docs/DEPLOYMENT.md 填写本机配置
.\.venv\Scripts\python.exe -m niguang365.webapp --init-auth
.\.venv\Scripts\python.exe -m niguang365.webapp --host 127.0.0.1 --port 8501
```

打开 `http://127.0.0.1:8501`。首次 `--init-auth` 只显示一次初始共享密码。

## 文档

- [文档索引](docs/README.md)
- [架构说明](docs/ARCHITECTURE.md)
- [配置与部署](docs/DEPLOYMENT.md)
- [主编用户手册](docs/USER_GUIDE.md)
- [API](docs/API.md)
- [迁移与回滚](docs/MIGRATION.md)
- [测试报告](docs/TEST_REPORT.md)
- [需求追踪 T-001—T-052](docs/REQUIREMENTS_TRACEABILITY.md)
- [设计系统与资源调研](docs/DESIGN_SYSTEM.md)

## 目录

```text
BKLT365/
  niguang365/
    core/         配置、SQLite、领域规则、仓储、适配器、服务、认证、调度
    webui/        新移动优先前端（index.html/style.css/app.js）
    tests/        90 项代码级测试（旧 + v2 + 迁移）
    webapp.py     FastAPI 主入口
    migrate_legacy.py 旧收件箱只读迁移
    config.example.json
  XiaohongshuSkills/  上游浏览器自动化子模块（MIT）
  docs/           架构、部署、用户、API、测试、迁移、追踪
```

## 测试

```powershell
& "C:\Users\chenquan\.workbuddy-ai\binaries\python\envs\xhs\Scripts\python.exe" `
  -m unittest discover -s niguang365/tests -v
```

最近结果：`Ran 90 tests ... OK`。详细范围与未执行项见 [测试报告](docs/TEST_REPORT.md)。

## 边界

- 飞书只读；不读取点赞/播放等互动数据。
- 原图不压缩；不上传真实投稿到公开仓库。
- 小红书草稿/发布由服务端 CDP 在项目 Chrome 中执行；发布必须主编在 Web 显式确认。
- 本机 `niguang365/config.json`、`niguang365/web.local.json` 已被忽略，不得提交。
- 真实飞书同步、小红书发布、123 上传、公网单会话尚未在本轮做真实平台验收；上线门禁见部署文档。

## 版本管理

- `main` 保持可运行、可回滚。
- 提交信息：`type(scope): summary`，例如 `feat(service): close week idempotently`。
- 高风险改动（批量改名、上传、发布）必须附测试证据与回滚说明。

## 第三方与许可

`XiaohongshuSkills/` 为 [white0dew/XiaohongshuSkills](https://github.com/white0dew/XiaohongshuSkills) 的 Git 子模块，遵循 MIT。其余代码的许可证由项目负责人另行确认。
