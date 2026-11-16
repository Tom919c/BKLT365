"""可靠的登录态检测。

小红书有**两套互相独立的登录态**，混用会得出错误结论：

| 用途 | 域 | 判断依据 |
|---|---|---|
| **创作者中心**（发布笔记） | `creator.xiaohongshu.com` | 创作者 cookie + 页面账号 / 发布入口 |
| 主页（检索 / 评论 / 通知） | `www.xiaohongshu.com` | ``web_session`` |

上游 ``cdp_publish._check_login`` 只判断 ``"login" in current_url``，
creator 首页不跳转时会**把未登录误报为已登录**（实测踩到过）。
本模块结合 cookie 与创作者页面证据；2026-11 实测创作者 token cookie
下线后，仍可通过账号信息、发布入口和 uid cookie 确认真实登录态。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from dataclasses import dataclass, field

import websockets

# 创作者中心登录凭证
CREATOR_TOKEN_COOKIE = "access-token-creator.xiaohongshu.com"
CREATOR_UID_COOKIE = "x-user-id-creator.xiaohongshu.com"
# 主页登录凭证
HOME_SESSION_COOKIE = "web_session"

XHS_DOMAINS = ("xiaohongshu.com", "xhscdn.com")
LOGIN_URL = "https://creator.xiaohongshu.com/login"
CREATOR_HOME = "https://creator.xiaohongshu.com/new/home"

# 主页鉴权接口：未登录返回 code -1
HOME_AUTH_PROBE_JS = """
(async () => {
  try {
    const r = await fetch(
      'https://edith.xiaohongshu.com/api/sns/web/v1/you/mentions?num=1&cursor=',
      {credentials: 'include'}
    );
    const j = await r.json();
    return JSON.stringify({ok: j.ok, code: j.code, msg: j.msg || ''});
  } catch (e) {
    return 'ERR:' + e.message;
  }
})()
"""

# 从创作者中心页面读账号信息，作为 cookie 之外的旁证
CREATOR_DOM_PROBE_JS = r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const m = text.match(/小红书账号[:：]\s*(\d+)/);
  const nick = text.match(/创作服务平台\n([^\n]+)/);
  return JSON.stringify({
    accountId: m ? m[1] : '',
    nickname: nick ? nick[1].trim() : '',
    hasPublishEntry: text.includes('发布笔记') || text.includes('发布图文笔记'),
    onLoginPage: location.pathname.includes('login')
  });
})()
"""


def list_targets(port: int = 9222) -> list[dict]:
    """列出 CDP 目标。显式绕过系统代理（否则 127.0.0.1 会被代理掉返回 502）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pick_page(targets: list[dict]) -> dict | None:
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        return None
    for page in pages:
        if "xiaohongshu" in page.get("url", ""):
            return page
    return pages[0]


async def _call(ws_url: str, method: str, params: dict, timeout: float = 20):
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg["result"]


def _call_sync(ws_url: str, method: str, params: dict):
    return asyncio.run(_call(ws_url, method, params))


def get_cookies(ws_url: str) -> list[dict]:
    return _call_sync(ws_url, "Network.getAllCookies", {}).get("cookies", [])


def evaluate(ws_url: str, expression: str):
    result = _call_sync(
        ws_url,
        "Runtime.evaluate",
        {"expression": expression, "awaitPromise": True, "returnByValue": True},
    )
    return result.get("result", {}).get("value")


def navigate(ws_url: str, url: str) -> None:
    _call_sync(ws_url, "Page.navigate", {"url": url})


@dataclass
class LoginStatus:
    reachable: bool
    creator_logged_in: bool = False
    home_logged_in: bool = False
    cookies: list[dict] = field(default_factory=list)
    page_url: str = ""
    account_id: str = ""
    nickname: str = ""
    dom_probe: dict = field(default_factory=dict)
    error: str = ""

    @property
    def logged_in(self) -> bool:
        """发布笔记只需要创作者中心登录态。"""
        return self.creator_logged_in

    def describe(self) -> str:
        if not self.reachable:
            return f"CDP 不可达：{self.error}"
        lines = [f"页面：{self.page_url}"]
        lines.append(
            "创作者中心：" + ("已登录" if self.creator_logged_in else "未登录")
            + (f"（{self.nickname} / {self.account_id}）" if self.account_id else "")
        )
        lines.append(
            "主页（检索/评论用）：" + ("已登录" if self.home_logged_in else "未登录")
        )
        if self.dom_probe:
            lines.append(f"页面旁证：{json.dumps(self.dom_probe, ensure_ascii=False)}")
        if not self.creator_logged_in:
            lines.append("→ 无法发布，请扫码登录创作者中心")
        return "\n".join(lines)


def _creator_dom_confirms_login(probe: dict, cookie_names: set[str]) -> bool:
    """创作者页面证据是否足以确认登录。

    2026-11 起，已登录页面可能只有 uid / web_session，不再有旧的
    ``access-token-creator.xiaohongshu.com``。此时必须同时看到发布入口，
    并有账号、昵称或 uid cookie 之一，避免把公开的发布页面误判为已登录。
    """
    if probe.get("onLoginPage") or not probe.get("hasPublishEntry"):
        return False
    return bool(
        probe.get("accountId")
        or probe.get("nickname")
        or CREATOR_UID_COOKIE in cookie_names
    )


def check(port: int = 9222) -> LoginStatus:
    """检测登录态。结合 cookie 和创作者页面，不只依赖 URL。"""
    try:
        targets = list_targets(port)
    except OSError as exc:
        return LoginStatus(reachable=False, error=str(exc))

    page = pick_page(targets)
    if not page:
        return LoginStatus(reachable=False, error="没有可用的 page target")

    ws_url = page["webSocketDebuggerUrl"]
    status = LoginStatus(reachable=True, page_url=page.get("url", ""))

    try:
        status.cookies = [
            c
            for c in get_cookies(ws_url)
            if any(d in c.get("domain", "") for d in XHS_DOMAINS)
        ]
    except Exception as exc:  # noqa: BLE001
        status.error = f"取 cookie 失败：{exc}"
        return status

    names = {c["name"]: (c.get("value") or "") for c in status.cookies}
    status.creator_logged_in = bool(names.get(CREATOR_TOKEN_COOKIE))
    status.home_logged_in = bool(names.get(HOME_SESSION_COOKIE))

    # 旧 creator token 已下线时，uid cookie 可作为导航到创作者中心检测的依据。
    if (
        "creator.xiaohongshu.com" not in status.page_url
        and not status.creator_logged_in
        and names.get(CREATOR_UID_COOKIE)
    ):
        try:
            navigate(ws_url, CREATOR_HOME)
            status.page_url = CREATOR_HOME
            time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001
            status.error = f"打开创作者中心失败：{exc}"
            return status

    # 页面在创作者中心时，用账号信息和发布入口补足 / 修正 cookie 结论。
    if "creator.xiaohongshu.com" in status.page_url:
        try:
            probe = {}
            deadline = time.monotonic() + 10
            while True:
                raw = evaluate(ws_url, CREATOR_DOM_PROBE_JS)
                probe = json.loads(raw) if raw else {}
                if probe and (
                    probe.get("onLoginPage")
                    or probe.get("hasPublishEntry")
                    or probe.get("accountId")
                ):
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.5)

            status.dom_probe = probe
            status.account_id = probe.get("accountId", "")
            status.nickname = probe.get("nickname", "")
            if probe.get("onLoginPage"):
                status.creator_logged_in = False
            elif _creator_dom_confirms_login(probe, set(names)):
                status.creator_logged_in = True
        except Exception as exc:  # noqa: BLE001
            status.dom_probe = {"error": str(exc)}

    return status


def probe_home_auth(port: int = 9222) -> str:
    """主页鉴权接口探测（检索/评论功能才需要）。"""
    try:
        page = pick_page(list_targets(port))
        if not page:
            return "无可用页面"
        return str(evaluate(page["webSocketDebuggerUrl"], HOME_AUTH_PROBE_JS))
    except Exception as exc:  # noqa: BLE001
        return f"探测失败：{exc}"


def open_login_page(port: int = 9222) -> LoginStatus:
    """把 Chrome 导航到登录页，方便扫码。"""
    try:
        targets = list_targets(port)
    except OSError as exc:
        return LoginStatus(reachable=False, error=str(exc))

    page = pick_page(targets)
    if not page:
        return LoginStatus(reachable=False, error="没有可用的 page target")

    navigate(page["webSocketDebuggerUrl"], LOGIN_URL)
    return check(port)
