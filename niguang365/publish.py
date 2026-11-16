"""调用上游 XiaohongshuSkills 发布推文。

要点：
1. 上游靠 ``PROGRAMFILES`` 环境变量找 chrome.exe，而 WorkBuddy 的 Git Bash
   **不导出**该变量，必须显式注入，否则报 "Chrome not found"。
2. 默认走 ``--preview``（只填表不点发布），确认无误后才切自动发布。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPO = Path(r"D:\Code\VSCode\BKLT365\XiaohongshuSkills")
DEFAULT_PYTHON = Path(
    r"C:\Users\chenquan\.workbuddy-ai\binaries\python\envs\xhs\Scripts\python.exe"
)


class PublishError(RuntimeError):
    """发布流程失败。"""


@dataclass
class PublishResult:
    ok: bool
    preview: bool
    returncode: int
    stdout: str
    stderr: str
    command: list[str]
    clicked: bool = False
    click_detail: str = ""
    page_state: dict | str | None = None

    def tail(self, lines: int = 25) -> str:
        text = (self.stdout or "") + (("\n" + self.stderr) if self.stderr else "")
        return "\n".join(text.strip().splitlines()[-lines:])


def _build_env() -> dict[str, str]:
    env = dict(os.environ)
    # 关键修复：补齐上游定位 Chrome 所需的环境变量
    env.setdefault("PROGRAMFILES", r"C:\Program Files")
    env.setdefault("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


DRAFT_STATE_JS = r"""
(() => {
  const titleInput = document.querySelector('div.d-input input') ||
    document.querySelector('input[placeholder*="标题"]');
  const editor = document.querySelector('div.tiptap.ProseMirror') ||
    document.querySelector('div.ProseMirror[contenteditable="true"]');
  const selectors = [
    '.img-preview-area .pr',
    ".img-preview-area [class*='preview']",
    '.draggable-item',
    "[class*='img-preview'] .pr"
  ];
  let images = 0;
  for (const selector of selectors) {
    try { images = Math.max(images, document.querySelectorAll(selector).length); }
    catch (e) {}
  }
  return JSON.stringify({
    ready: !!document.querySelector('input[type="file"]'),
    images,
    title: titleInput ? titleInput.value.trim() : '',
    content: editor ? editor.innerText.trim() : ''
  });
})()
"""


def _draft_has_content(state: dict) -> bool:
    return bool(
        int(state.get("images") or 0)
        or (state.get("title") or "").strip()
        or (state.get("content") or "").strip()
    )


def draft_has_content(state: dict | None) -> bool:
    """公开的草稿非空判断，供 Web 发布前分支使用。"""
    return _draft_has_content(state or {})


def get_current_draft(port: int = 9222) -> dict | None:
    """读取当前已打开的发布页草稿；没有发布页时返回 None。"""
    from .logincheck import evaluate, list_targets

    targets = list_targets(port)
    pages = [t for t in targets if t.get("type") == "page"]
    publish_pages = [t for t in pages if "/publish/publish" in t.get("url", "")]
    if not publish_pages:
        return None
    raw = evaluate(publish_pages[0]["webSocketDebuggerUrl"], DRAFT_STATE_JS)
    if not raw:
        raise PublishError("发布页没有返回草稿状态")
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PublishError("无法解析发布页草稿状态") from exc
    return state if isinstance(state, dict) else None


def _flat(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def draft_mismatch(state: dict, title: str, content: str) -> str:
    """返回草稿与目标文案不一致的原因；一致时返回空字符串。"""
    actual_title = (state.get("title") or "").strip()
    if actual_title != title.strip():
        return (
            "当前小红书草稿的标题与所选投稿不一致："
            f"草稿为 {actual_title or '空'}，目标为 {title}。"
        )

    actual_body = _flat(state.get("content"))
    required_lines = [
        _flat(line)
        for line in content.splitlines()
        if _flat(line) and not _flat(line).startswith("#")
    ]
    for line in required_lines:
        if line not in actual_body:
            return f"当前小红书草稿缺少正文内容：{line}"

    tags = [
        word
        for line in content.splitlines()
        for word in line.split()
        if word.startswith("#")
    ]
    for tag in tags:
        if tag.lstrip("#") not in actual_body:
            return f"当前小红书草稿缺少话题标签：{tag}"

    if int(state.get("images") or 0) <= 0:
        return "当前小红书草稿没有图片，不能发布。"
    return ""


def page_state_is_published(page_state: str | dict | None) -> bool:
    """判断点击发布后页面是否进入成功状态。"""
    if isinstance(page_state, str):
        try:
            state = json.loads(page_state)
        except json.JSONDecodeError:
            return False
    elif isinstance(page_state, dict):
        state = page_state
    else:
        return False
    return bool(
        "/publish/success" in (state.get("url") or "")
        or state.get("hasSuccessHint")
    )


def publish_current_draft(
    *, title: str, content: str, port: int = 9222
) -> dict:
    """确认当前草稿与目标一致后，点击已有发布页的「发布」按钮。

    这是不可逆的外部动作。调用前必须由用户明确确认；本函数不会清空草稿，
    也不会在结果不确定时自动重试。
    """
    state = get_current_draft(port)
    if not state:
        raise PublishError("没有已打开的发布页，请先填入预览。")
    if not _draft_has_content(state):
        raise PublishError("当前发布页草稿为空，请先填入预览。")
    mismatch = draft_mismatch(state, title, content)
    if mismatch:
        raise PublishError(mismatch + " 请先放弃当前草稿并从 Web 重新填入。")

    from .publishbtn import PublishButtonError, click_publish

    try:
        detail = click_publish(port)
    except PublishButtonError as exc:
        message = str(exc)
        clicked: bool | None = None
        if "未找到发布页" in message or "未找到发布按钮" in message:
            clicked = False
        return {
            "ok": False,
            "clicked": clicked,
            "published": False,
            "needs_review": clicked is None,
            "page_state": None,
            "message": f"点击发布失败：{message}",
        }

    page_state = detail.get("page_state")
    if isinstance(page_state, str):
        try:
            page_state = json.loads(page_state)
        except json.JSONDecodeError:
            pass
    published = page_state_is_published(page_state)
    return {
        "ok": published,
        "clicked": True,
        "published": published,
        "needs_review": not published,
        "page_state": page_state,
        "message": (
            "已发布成功。"
            if published
            else "已点击发布，但页面未返回成功状态；请立即到小红书后台核对，不要重复点击。"
        ),
    }


def ensure_empty_draft(port: int = 9222) -> dict:
    """发布前确认草稿为空，防止重复预览累积图片或覆盖旧文案。"""
    from .logincheck import evaluate, list_targets, navigate, pick_page

    targets = list_targets(port)
    pages = [t for t in targets if t.get("type") == "page"]
    publish_pages = [t for t in pages if "/publish/publish" in t.get("url", "")]
    if publish_pages:
        page = publish_pages[0]
    else:
        page = pick_page(targets)
        if not page:
            raise PublishError("没有可用的 Chrome 页面来检查小红书草稿")
        navigate(
            page["webSocketDebuggerUrl"],
            "https://creator.xiaohongshu.com/publish/publish?source=official",
        )

    ws_url = page["webSocketDebuggerUrl"]
    state: dict = {}
    deadline = time.monotonic() + 15
    while True:
        raw = evaluate(ws_url, DRAFT_STATE_JS)
        state = json.loads(raw) if raw else {}
        if state.get("ready") or time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    if _draft_has_content(state):
        raise PublishError(
            "检测到未清空的小红书草稿（"
            f"图片 {state.get('images') or 0} 张，"
            f"标题 {state.get('title') or '空'}）。"
            "为避免覆盖旧内容或重复上传，已停止。"
            "请人工清空/放弃该草稿，新建空白草稿后重试。"
        )
    return state


def publish_note(
    *,
    title: str,
    content: str,
    image: str | Path | None = None,
    images: list[str | Path] | None = None,
    preview: bool = True,
    headless: bool = True,
    account: str | None = None,
    post_time: str | None = None,
    repo: str | Path = DEFAULT_REPO,
    python: str | Path = DEFAULT_PYTHON,
    timeout: int = 600,
    require_login: bool = True,
    save_draft: bool = False,
    check_empty: bool = True,
    port: int = 9222,
) -> PublishResult:
    """发布一篇图文笔记。

    填表始终通过上游 ``--preview`` 完成（不点发布）；当 ``preview=False`` 时，
    再由本模块的 ``publishbtn`` 用 CDP 穿透 shadow DOM 点击「发布」按钮。

    Args:
        title: 标题（不含书名号也可，上游原样填入）。
        content: 正文，最后一行必须是 ``#标签`` 形式。
        image: 本地图片绝对路径。
        preview: True 只填表不发布（调试期务必为 True）。
        headless: 无头模式。未登录时会自动回落为有窗口。
        account: 多账号名，None 用默认账号。
        post_time: 定时发布时间，格式见上游 ``--post-time``。
        repo: 上游仓库根目录。
        python: 运行上游脚本的解释器。
        timeout: 超时秒数。
        require_login: 发布前先做真实登录校验，未登录直接报错。

    Returns:
        PublishResult
    """
    if require_login:
        from .logincheck import check as _probe

        status = _probe(port)
        if not status.logged_in:
            raise PublishError(
                "小红书未登录，已中止发布。\n" + status.describe()
            )

    if not images:
        images = [image] if image is not None else []
    if not images:
        raise PublishError("至少需要一张图片")

    if check_empty:
        ensure_empty_draft(port)

    repo = Path(repo)
    script = repo / "scripts" / "publish_pipeline.py"
    if not script.is_file():
        raise PublishError(f"找不到上游脚本：{script}")

    image_paths = [Path(item).resolve() for item in images]
    for image_path in image_paths:
        if not image_path.is_file():
            raise PublishError(f"图片不存在：{image_path}")

    # 填表始终走 --preview。
    # 原因：上游的点击逻辑找不到发布按钮（按钮在 <xhs-publish-btn> 的
    # closed shadow root 里），--auto 会抛 "Could not find publish button"。
    # 所以点击交给我们自己的 publishbtn 模块用 CDP 穿透完成。
    cmd: list[str] = [
        str(python),
        str(script),
        "--title",
        title,
        "--content",
        content,
        "--images",
        *[str(path) for path in image_paths],
        "--preview",
    ]
    if headless:
        cmd.append("--headless")
    if account:
        cmd.extend(["--account", account])
    if post_time:
        cmd.extend(["--post-time", post_time])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            env=_build_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishError(f"发布超时（{timeout}s）") from exc

    stdout = proc.stdout or ""
    filled = proc.returncode == 0 and "NOT LOGGED IN" not in stdout

    result = PublishResult(
        ok=filled,
        preview=preview,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=proc.stderr or "",
        command=cmd,
    )

    if not filled:
        return result

    if preview:
        if save_draft:
            from .publishbtn import PublishButtonError, click_save_draft

            try:
                detail = click_save_draft(port)
                result.clicked = True
                result.click_detail = f"已点击暂存离开：{detail}"
                result.page_state = detail.get("page_state")
            except PublishButtonError as exc:
                raise PublishError(f"填表成功但保存草稿失败：{exc}") from exc
        return result

    # 真正点发布
    from .publishbtn import PublishButtonError, click_publish

    try:
        detail = click_publish(port)
        result.clicked = True
        result.click_detail = str(detail)
        result.page_state = detail.get("page_state")
    except PublishButtonError as exc:
        result.ok = False
        result.click_detail = f"点击失败：{exc}"
        raise PublishError(f"填表成功但点击发布失败：{exc}") from exc

    return result


def check_login(
    *,
    repo: str | Path = DEFAULT_REPO,
    python: str | Path = DEFAULT_PYTHON,
    account: str | None = None,
    timeout: int = 120,
) -> dict:
    """检测登录态。

    **不使用上游的判定结果** —— 上游只看 URL 里有没有 "login"，
    creator 首页不跳转时会把未登录误报为已登录。这里改查真实凭证。

    Returns:
        {'logged_in': bool, 'raw': str, 'detail': str}
    """
    from .logincheck import check as _probe

    status = _probe()
    return {
        "logged_in": status.logged_in,
        "raw": status.describe(),
        "detail": status,
    }


if __name__ == "__main__":  # 手工排查用
    print(json.dumps(check_login(), ensure_ascii=False, indent=2))
    sys.exit(0)
