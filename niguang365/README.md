# 逆光365 自动化

电子科技大学「逆光摄影社团」每日图片收集、归档与小红书自动发布。

底座是开源项目 [`white0dew/XiaohongshuSkills`](../XiaohongshuSkills)（Chrome CDP 驱动真实浏览器）。
本目录负责**飞书取图、命名规范、文案生成、调用发布和 123 云盘文件夹上传**。

| 文件 | 职责 |
|---|---|
| `feishu.py` | 默认通过 `lark-cli` 官方 API 读取记录并下载附件；旧 CDP 取数仅作备用 |
| `naming.py` | 文件名规范解析/生成；容错全半角括号、带编号入选、图名含下划线 |
| `compose.py` | 文案生成；本地复现上游标签提取逻辑，不合规**直接报错** |
| `publish.py` | 封装上游 CLI；注入 `PROGRAMFILES`；发布前强制真实登录校验 |
| `publishbtn.py` | CDP 穿透 closed shadow root 定位并点击「发布」按钮 |
| `collect.py` | 收件箱扫描、入选判定、重命名归档 + `manifest.json` |
| `logincheck.py` | 可靠登录检测，区分创作者中心 / 主页两套登录态 |
| `pan123.py` | 通过 Playwright/CDP 上传整个文件夹到 123 云盘并回读核验 |
| `cli.py` | 飞书、归档、小红书发布及 123 云盘上传命令入口 |
| `web.py` | FastAPI 手机选图、登录保护、入选清单与小红书预览接口 |
| `static/` | 无外部前端依赖的手机 HTML/CSS/JS 页面 |

---

## 快速开始

前置条件：`lark-cli` 可用且当前飞书账号有表格阅读权限；小红书已在项目专用 Chrome 中扫码登录。

```bash
PY="C:/Users/chenquan/.workbuddy-ai/binaries/python/envs/xhs/Scripts/python.exe"

$PY -m niguang365 daily --date 20260915              # 一条命令跑完全流程（推荐）
$PY -m niguang365 daily --date 20260915 --auto       # 同上，且真发布

$PY -m niguang365 check-login                    # 检测小红书登录态
$PY -m niguang365 open-login                     # 打开小红书登录页以便扫码
$PY -m niguang365 pan123-login                   # 打开 123 云盘登录页以便扫码
$PY -m niguang365 pan123-upload "D:\工作文件\逆光\365\20261109-1115"
$PY -m niguang365 fetch --list-only              # 看飞书收集表提交了什么
$PY -m niguang365 fetch --rename convention      # 只下载并重命名
$PY -m niguang365 scan      --date 20260915      # 列出当日候选
$PY -m niguang365 dry-run   --date 20260915      # 预览将发布的文案
$PY -m niguang365 archive   --date 20260915      # 重命名并归档
$PY -m niguang365 publish   --date 20260915          # 填表，不点发布
$PY -m niguang365 publish   --date 20260915 --auto   # 真发布
```

### daily 的四步

```
[1/4] 从飞书拉取投稿     → 收件箱/<日期>/
[2/4] 扫描收件箱         → 列出候选与入选
[3/4] 归档               → 已归档/<年>/<日期>/
[4/4] 发布               → 填表（--auto 才真点发布）
```

主编标记入选的三种方式：文件名加 `（入选）`、把照片拖进
`收件箱/<日期>/入选/` 子目录，或在手机 Web 页面勾选。Web 清单存在时以清单为准。

测试：

```bash
cd D:/Code/VSCode/BKLT365
$PY -m unittest discover -s niguang365/tests -t .
```

---

## 手机选图 Web（MVP）

主编只需浏览器，不需要安装 App、VPN 或接触命令行。后端复用现有
`collect.py`、`compose.py` 和 `publish.py`，不另写一套发布逻辑。

### 启动

```powershell
cd D:/Code/VSCode/BKLT365
$PY = "C:/Users/chenquan/.workbuddy-ai/binaries/python/envs/xhs/Scripts/python.exe"

# 仅首次：生成随机初始密码、密码哈希和会话密钥
$PY -m niguang365.web --init-auth

# 只监听本机回环地址，供 Cloudflare Tunnel 转发
$PY -m niguang365.web --host 127.0.0.1 --port 8501
```

本机打开 `http://127.0.0.1:8501`。登录配置保存在
`niguang365/web.local.json`，该文件已被 `.gitignore` 排除，不要复制到公开位置。
需要重置密码时，先停止 Web 服务，备份并移走该文件，再重新执行 `--init-auth`；
旧会话会同时失效。

### 页面流程

1. 输入访问密码；连续输错 5 次会临时锁定 5 分钟。
2. 选择投稿日期，查看原图；浏览器只缩放显示，不修改图片文件。
3. 按需点击“同步飞书”，通过官方 API 读取新投稿；飞书侧保持只读。
4. 勾选入选并点击“保存并生成预览”。系统写入：
   - `收件箱/<日期>/.niguang365-selection.json`：入选相对路径清单；
   - `收件箱/<日期>/.niguang365-preview.json`：标题、正文、标签和告警。
5. 页面立即显示小红书发布预览。此时**尚未填入小红书，更没有发布**。
6. 需要真实填表时，再点击单篇“仅填入后台预览（不发布）”。后端固定调用
   `publish_note(preview=True)`，不会点击发布。填表前项目专用 Chrome 必须在
   `9222` 运行并保持小红书登录，否则会提示 CDP 不可达。
7. 在后台核对图片、标题、正文和标签无误后，点击“确认发布到小红书”。页面会
   再弹一次确认；后端在点击前重新读取当前草稿，只有标题、正文、标签和图片
   都与所选稿件一致才会点击原生发布按钮。刷新页面后会自动做一次只读草稿
   检查，匹配时直接启用发布按钮，不匹配时显示具体差异。
8. 发布成功后写入 `.niguang365-published.json`，同一篇会被锁定，不能重复发布。
   若已点击但页面未返回成功状态，页面会标记“发布状态待人工确认”，不会自动重试。

原图始终只读，不压缩、不改尺寸比例、不加水印。Web 清单会同时被现有
`scan` / `dry-run` / `publish` 命令识别，所以网页和命令行不会各保存一套结果。

### 发布状态文件

- `.niguang365-selection.json`：入选清单。
- `.niguang365-preview.json`：本地文案预览，`published` 字段不代表真实发布。
- `.niguang365-published.json`：真实发布记录，保存标题、发布时间、笔记链接和
  成功页地址。页面据此禁用已发布稿件。

原图始终只读，不压缩、不改尺寸比例、不加水印。Web 清单会同时被现有
`scan` / `dry-run` / `publish` 命令识别，所以网页和命令行不会各保存一套结果。

### Cloudflare Tunnel

正式路由为 `365.niguang.me -> http://localhost:8501`。8501 不需要向局域网或
路由器开放，也不要改为监听 `0.0.0.0`。上线前仍需：

- 2026-11-16 已实测 `https://365.niguang.me/api/health` 返回 200，公网 HTTPS 登录和投稿读取正常；仍需再用手机蜂窝网络验收；
- 发布接口已实装但仍需在测试账号做一次真实发布验收，并确认成功页和笔记链接；
- 把 Web 进程配置成 Windows 服务或开机计划任务，并验证重启自动恢复；
- 把项目专用 Chrome 也纳入开机启动并保持 `9222` 可访问，或由管理员在填表前启动；
- 妥善更换初始密码；需要按人授权时可再加 Cloudflare Access；
- 真实填表先验证一篇，正式发布仍保留管理员人工确认。

---

## 飞书收集表取图

`fetch` 从飞书多维表格拉取投稿图片，落到 `收件箱/<YYYYMMDD>/`。

### 官方 API（默认）

`fetch` 默认通过 `lark-cli` 调用飞书官方多维表格接口，当前飞书账号必须拥有该表的阅读权限。飞书取数不依赖 Chrome。

```json
"feishu": {
  "backend": "official",
  "cli_path": "lark-cli",
  "app_token": "<表格 token>",
  "table_id": "<数据表 ID>",
  "view_id": "<视图 ID>",
  "name_field_id": "fldzKqqKKW",
  "title_field_id": "fldWYlP7uA",
  "rename": "convention"
}
```

记录读取使用 `+record-list`，按 `field_id_list` 映射列并完整翻页；附件使用
`+record-download-attachment`。附件先落到 `%LOCALAPPDATA%\Temp` 下的隔离目录，
校验文件大小后再移动到收件箱，同名不同大小的文件不会覆盖。重复点击“同步飞书”
时，最终命名目标已存在且大小相同的图片会直接跳过，不再生成 `_1` / `_2` 副本。

2026-11-15 实测：当前账号可读取记录，两张附件均下载成功且可正常解码。
旧网页内部接口当日返回 404，因此 `backend: "legacy"` 只保留作排查备用。

### 字段识别（务必用显式 ID）

**不要依赖启发式。** 多维表格列顺序变化时，「取第一个含中文字符的文本字段」
可能把图名当成姓名。配置里的 `name_field_id` / `title_field_id` 是主路径；
启发式只在未配置时兜底：姓名优先含中日韩字符的文本，图名排除姓名、纯数字和纯日期。

### 命名规则

| 情况 | 产出 |
|---|---|
| 有图名，单图 | `20260915_陈权_我的一天.jpg` |
| 有图名，多图 | `20260915_陈权_我的一天.jpg`、`20260915_陈权_我的一天02.jpg` |
| 无图名 | `20260915_陈权_01.jpg`（序号占位，需主编补图名） |



---

## 123 云盘：整个文件夹上传

走项目专用 Chrome 的 CDP（默认端口 `9222`），**不使用开放 API，不需要开发者权益包**。
文件通过 Chrome 原生能力按本地路径上传，不压缩、不改写原图。

```powershell
# 首次使用或登录失效后：在项目 Chrome 中扫码
$PY -m niguang365 pan123-login

# 上传整个文件夹到云盘根目录并回读核验
$PY -m niguang365 pan123-upload "D:\工作文件\逆光\365\20261109-1115"

# 也可以在 daily 流程结束后附加上传
$PY -m niguang365 daily --upload-pan123 "D:\工作文件\逆光\365\20261109-1115"
```

上传行为：

- 用户和脚本始终以**文件夹**为单位，不需要逐个选择文件；
- 自动创建目标目录及嵌套子目录，批量上传其中的文件；
- 同名文件夹已存在时，只补传缺失文件，已有同名文件不动，云盘额外文件不删除；
- 核验每个文件的名称、页面显示大小和目录结构；未做内容哈希校验；
- 重复执行同一命令时，内容一致则只核验，不重复上传；
- 登录过期时重新执行 `pan123-login` 扫码即可。

`pan123.cdp_port` 和 `pan123.upload_timeout` 可在 `config.json` 中调整。
该功能依赖云盘网页结构，网站改版后可能需要同步维护 `pan123.py` 中的选择器。

---

## 目录契约

```
<root>/                              # 默认 D:\逆光365
  收件箱/                             # 飞书自动下载落点，本工具只读
    20260915/                         # 当日文件夹；图片平铺在 收件箱/ 下也兼容
      王璇_谁在树下弹吉他.jpg
      submissions.csv                 # 飞书导出，列名见下
      入选/                            # 主编把选中照片拖进来
      .niguang365-selection.json       # Web 入选清单（存在时优先）
      .niguang365-preview.json         # Web 生成的文案预览，不代表已发布
      .niguang365-published.json       # Web 真实发布记录，防重复发布
    摄影师账号.csv                     # 可选，摄影师 -> 小红书号 兜底映射
  已归档/
    2026/
      20260915/
        20260915_王璇_谁在树下弹吉他.jpg
        manifest.json                 # 归档清单，含小红书号与图说
```

### submissions.csv 列名

模糊匹配，中英文都认：

| 字段 | 可接受的列名 |
|---|---|
| 摄影师 | 摄影师 / 作者 / 姓名 / photographer / author |
| 图名 | 图名 / 作品名 / 标题 / title / name |
| 小红书号 | 小红书号 / 小红书 / 账号 / handle / xhs / redbook |
| 图说 | 图说 / 文案 / 说明 / quote / caption / desc |

匹配优先级：先按 `(摄影师, 图名)` 精确匹配，失败再退回 `摄影师账号.csv` 的摄影师级映射。

---

## 命名规范

`YYYYMMDD_摄影师_图名（入选）.jpg`

- 分隔符固定**半角下划线**
- 图名含下划线也能解析（第 3 段之后全部并入图名）
- 入选标记容错：全角/半角括号、带编号（`（1号入选）`）
- **入选判定两种方式**：文件名带 `（入选）`，或文件位于 `入选/` 子目录

---

## 推文格式

```
你的笑脸拨动我的琴弦          ← 图说
摄影师：王璇@wangxuan_123      ← @是纯文本，见下方「已知限制」
2026年9月15日
#电子科技大学 #逆光日记        ← 必须是最后一个非空行
```

### 为什么标签必须在最后一行

上游 `publish_pipeline._extract_topic_tags_from_last_line` 的实现：

```python
lines = content.splitlines()
last_line = lines[-1].strip()
parts = last_line.split()
if not all(re.fullmatch(r"#[^\s#]+", part) for part in parts):
    return content, []          # 静默放弃，标签行会当普通文字留在正文
```

两个硬条件：**必须是最后一个非空行**，**标签内不能有第二个 `#`**。

所以社团须知原本的写法 `#电子科技大学##逆光日记#` 放在倒数第二行，结果是
**一个标签都提取不到**，整行变成死文字。实测对照：

| 格式 | 提取结果 |
|---|---|
| 须知原格式 | `[]` |
| 调整后格式 | `['#电子科技大学', '#逆光日记']` |

`compose.py` 会在本地复现这套逻辑并**直接报错**，不会让问题流到线上。

---

## 已知限制

### `@账号` 是纯文本，不会真正 @ 到人

上游 `cdp_publish._fill_content` 是直接注入 DOM：

```js
paragraph.textContent = line;   // 不是键盘输入
```

好处是不会触发 @ 联想弹窗、不卡流程；代价是小红书不会把它解析成 mention 节点，
**对方收不到通知，也不可点击**。

对比：话题标签走的是真键盘输入 + `Enter` 确认（`_select_topics`），所以标签能生效 ——
实测渲染成 `<a class="tiptap-topic" data-topic='{"name":"电子科技大学"}'>`，可点击。

### 上游的 `--auto` 点不动发布按钮（本模块已绕过）

小红书把发布按钮封装进了自定义元素 `<xhs-publish-btn>`，其 **shadow root 是 closed 的**。
`document.querySelector` 系列 API 无法穿透，所以上游
`_get_publish_button_rect` 返回 `null`，`--auto` 必然抛
`Could not find publish button`。

实测证据（CDP 穿透后的 DOM 树）：

| 元素 | 位置 | 是什么 |
|---|---|---|
| `BUTTON.ce-btn.white` | 430, 803 | 暂存离开 |
| `BUTTON.ce-btn.bg-red` | 574, 803 | **发布** |

类名 `bg-red` 跟上游选择器一致，但它在封闭 shadow root 里。

**本模块的处理**：填表始终走上游 `--preview`，点击交给 `publishbtn.py`，
用 CDP `DOM.getDocument(pierce=True)` + `DOM.getBoxModel` + `Input.dispatchMouseEvent` 完成。

```bash
# 只定位不点击
python -m niguang365.publishbtn

# 真实点击（会发布！）
python -m niguang365.publishbtn --click
```

### 登录检测不能用上游的

上游 `_check_login` 只判断 `"login" in current_url`。creator 首页不跳转时会
**把未登录误报为已登录**（本机实测踩到过，报 `Login confirmed` 但实际没有凭证）。

另外小红书有**两套独立登录态**，混用会得出错误结论：

| 用途 | 域 | 关键 cookie |
|---|---|---|
| **创作者中心**（发布笔记） | `creator.xiaohongshu.com` | `access-token-creator.xiaohongshu.com` |
| 主页（检索 / 评论 / 通知） | `www.xiaohongshu.com` | `web_session` |

本工具结合 cookie 与创作者页面证据分别判定，**发布只要求创作者中心登录态**。
2026-11 实测旧的 `access-token-creator.xiaohongshu.com` 已不再出现，因此还会读取
账号/昵称和发布入口，并用 `x-user-id-creator.xiaohongshu.com` 辅助确认；明确的登录页始终判为未登录。

### 非空草稿会先停止

重复预览可能让图片累积到同一个线上草稿。`publish.py` 现在会在填表前检查标题、正文和图片；
只要已有内容就停止，不会自动删除或覆盖。需要人工清空/放弃旧草稿并新建空白草稿后再运行。

---

## 环境坑（都已在本工具内规避）

| 坑 | 现象 | 规避方式 |
|---|---|---|
| Git Bash 不导出 `PROGRAMFILES` | 上游报 "Chrome not found" | `publish.py` 注入该变量 |
| **Bash 工具回收 Chrome 进程** | 启动后几秒消失，端口 9222 无人监听 | WorkBuddy 里必须用 `run_in_background`；普通终端用 `start-browser` |
| 系统代理拦回环请求 | 访问 `127.0.0.1:9222` 返回 502 | 显式 `ProxyHandler({})` |
| 同 profile 重复启动 | 新进程只是转发 URL 后退出，调试端口不开 | 先确认 9222 无监听再启动 |

Chrome 启停统一走 `browser.py`：

```bash
$PY -m niguang365 start-browser status     # 或 start / stop / restart
```

`daily` 会自动检查 CDP 可用性。四种启动方式（普通 Popen、分离标志、
ShellExecute、schtasks）实测在 WorkBuddy 里都会被回收或拦截，
**只有 Bash 工具的 `run_in_background: true` 可行** —— 详见 `browser.py` 顶部注释。

---

## 配置

复制 `config.example.json` 为 `config.json` 后按需修改。字段：

| 字段 | 说明 |
|---|---|
| `root` | 项目根目录 |
| `repo` | 上游仓库路径 |
| `python` | 运行上游脚本的解释器 |
| `account` | 上游多账号名，`null` 用默认 |
| `roster` | 摄影师 -> 小红书号 映射文件 |
| `tags` | 话题标签，**必须带 `#`** |
| `post_time` | 定时发布时间，透传上游 `--post-time` |
| `feishu.backend` | `official`（默认，官方 API）或 `legacy`（旧网页接口备用） |
| `feishu.cli_path` | `lark-cli` 可执行文件，默认从 PATH 查找 |
| `pan123.cdp_port` | 项目 Chrome 的 CDP 端口，默认 `9222` |
| `pan123.upload_timeout` | 文件夹上传与核验总超时秒数，默认 `1800` |

---

## 验证状态

2026-11-15 再次联调通过：

| 环节 | 状态 | 证据 |
|---|---|---|
| 离线回归 | ✅ | 56 个单元测试全部通过 |
| 飞书记录与附件 | ✅ | 官方 API 读取 1 条记录，2 张附件下载成功且大小一致 |
| 小红书登录检测 | ✅ | 新 Cookie 形态下正确识别已登录 |
| 飞书 → 命名 → 归档 | ✅ | 隔离目录 `daily` 下载 2 张并规范归档 |
| 入选 → 填表预览 | ✅ | 隔离目录 `daily` 返回成功，未点击发布 |
| 标题/正文/图片/话题 | ✅ | 《我的一天》预览达到 `READY_TO_PUBLISH` |
| 占位账号处理 | ✅ | `请填小红书号` 不再进入正文 |
| 非空草稿保护 | ✅ | 已有草稿会停止填表，实测拦截成功，不自动删除 |
| 123 文件夹上传 | ✅ | 16 个原图名称、大小和目录结构核验一致，重复执行不重复上传 |

历史验证：2026-09-15 曾用测试号真实发布成功，跳转 `publish/success`，笔记管理可见。

## 尚未实现

- 123 云盘每周自动上传调度（独立上传命令已实测可用）
- 微信群通知（已决定暂缓，改主编手动转发）
- AI 挑选照片（已决定不做，主编标记 `（入选）` 即可）
- 定时发布（上游 `--post-time` 可用，尚未接入调度）
