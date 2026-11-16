# BKLT365 / 逆光365

电子科技大学逆光摄影社团「365 日更」项目的投稿收集、组图编排、发布准备与周归档工作台。

> 当前仓库处于**重构前基线 + 任务书阶段**：现有代码仍按旧流程运行，目标业务规则以《逆光365系统重构与软件工程化任务书》为准；在任务书执行完成前，不得把现有页面当作新流程已交付。

## 文档入口

- [逆光365系统重构与软件工程化任务书](逆光365系统重构与软件工程化任务书.docx)：目标业务规则、架构、逐条任务与验收门禁。
- [逆光365软件需求规格说明书_现状需求基线](逆光365软件需求规格说明书_现状需求基线.docx)：现状证据，不是已签认的目标需求。
- [交接说明](交接说明.md)：历史运行方式、关键路径与已踩过的坑。
- [逆光365自动化方案](逆光365自动化方案.md)：早期自动化方案记录。
- [niguang365/README.md](niguang365/README.md)：现有应用的运行与命令说明。
- [docs/README.md](docs/README.md)：正式文档索引与后续补充位置。

## 目录结构

```text
BKLT365/
  niguang365/              现有 FastAPI 应用、静态页面、测试与运行说明
  XiaohongshuSkills/       上游浏览器自动化能力，作为 Git 子模块引入（MIT）
  tools/                   飞书、登录与发布页诊断脚本
  交接说明.md              历史交接记录
  逆光365自动化方案.md     早期方案
  逆光365系统重构与软件工程化任务书.docx
  逆光365软件需求规格说明书_现状需求基线.docx
  docs/                    正式文档索引与后续文档目录
```

## 获取代码

```powershell
git clone --recurse-submodules https://github.com/Tom919c/BKLT365.git
cd BKLT365
```

如果已经克隆但子模块为空：

```powershell
git submodule update --init --recursive
```

## 配置

仓库只提交配置模板；实际运行配置和认证秘密留在本机：

```powershell
Copy-Item niguang365\config.example.json niguang365\config.json
```

- `niguang365/config.json`：本机数据根、飞书表坐标、浏览器与上传参数，已被忽略。
- `niguang365/web.local.json`：Web 登录口令哈希与会话密钥，已被忽略。
- 不得把真实投稿图片、浏览器 Cookie、飞书凭据、123 登录态或数据库提交到仓库。

## 安全与数据边界

- 正式账号发布必须由主编在工作室电脑的原生页面人工点击；代码不得自动点击发布。
- 不读取点赞、播放、收藏、评论等互动数据。
- 不把真实投稿、个人信息或本地绝对路径写入公开日志。
- 本仓库不承担图片存储；数据目录与备份按部署文档单独管理。

## 版本管理约定

- `main` 保持可运行、可回滚；不直接堆放未完成实验。
- 开发任务使用分支：`task/T-001-business-rules`、`task/T-026-browser-prefill` 等。
- 提交信息使用 `type(scope): summary`，例如 `feat(compose): add caption block`、`docs(taskbook): clarify week lifecycle`。
- 高风险改动（批量改名、上传、发布准备）必须附带测试证据与回滚说明。
- 历史文档中如出现旧路径 `BKLTxhs`，仅为当时记录；当前仓库与部署路径以 `BKLT365` 为准。

## 第三方与许可

`XiaohongshuSkills/` 为上游仓库 [white0dew/XiaohongshuSkills](https://github.com/white0dew/XiaohongshuSkills) 的 Git 子模块，遵循其 MIT 许可证。其余项目代码的许可证与对外发布范围由项目负责人另行确认。
