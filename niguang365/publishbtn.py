"""绕过 shadow DOM 定位并点击「发布」按钮。

背景：小红书创作者中心把发布按钮封装进了自定义元素 ``<xhs-publish-btn>``，
其 **shadow root 是 closed 的**。``document.querySelector`` 系列 API 无法穿透，
所以上游 ``cdp_publish._get_publish_button_rect`` 永远找不到按钮，
``--auto`` 会抛 ``Could not find publish button``。

CDP 的 ``DOM.getDocument(pierce=True)`` 有特权，能拿到 shadow root 内部节点，
再用 ``DOM.getBoxModel`` 取坐标，最后用 ``Input.dispatchMouseEvent`` 点击。

已实测按钮特征：``BUTTON.ce-btn.bg-red``（文案「发布」），
同级 ``BUTTON.ce-btn.white`` 是「暂存离开」。
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass

import websockets

# 发布按钮：红色主按钮；同名 class 的白色按钮是「暂存离开」
PUBLISH_BTN_CLASS = "bg-red"
PUBLISH_BTN_TAG = "button"

# 自定义元素宿主
HOST_TAG = "xhs-publish-btn"

DEFAULT_PORT = 9222


@dataclass
class ButtonRect:
    x: float
    y: float
    width: float
    height: float
    label: str = ""

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)

    def __str__(self) -> str:
        cx, cy = self.center
        return (
            f"{self.label or 'button'} @ ({cx:.0f}, {cy:.0f}) "
            f"size {self.width:.0f}x{self.height:.0f}"
        )


class PublishButtonError(RuntimeError):
    pass


def _list_targets(port: int) -> list[dict]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick_publish_page(targets: list[dict]) -> dict | None:
    pages = [t for t in targets if t.get("type") == "page"]
    for page in pages:
        if "/publish" in page.get("url", ""):
            return page
    return None


class _Session:
    """一次 CDP 连接，顺序发命令。"""

    def __init__(self, ws_url: str):
        self._ws_url = ws_url
        self._seq = 0

    async def __aenter__(self):
        self._sock = await websockets.connect(
            self._ws_url, max_size=300 * 1024 * 1024
        )
        await self.call("DOM.enable")
        return self

    async def __aexit__(self, *exc):
        await self._sock.close()

    async def call(self, method: str, params: dict | None = None) -> dict:
        self._seq += 1
        msg_id = self._seq
        await self._sock.send(
            json.dumps({"id": msg_id, "method": method, "params": params or {}})
        )
        while True:
            msg = json.loads(await self._sock.recv())
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise PublishButtonError(
                        f"{method} 失败：{msg['error']}"
                    )
                return msg.get("result", {})


def _walk(node: dict, out: list[dict], depth: int = 0) -> None:
    """递归遍历穿透后的 DOM 树（含 shadowRoots）。"""
    if depth > 60:
        return
    out.append(node)
    for key in ("children", "shadowRoots", "contentDocument"):
        child = node.get(key)
        if isinstance(child, dict):
            _walk(child, out, depth + 1)
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, dict):
                    _walk(item, out, depth + 1)


def _attrs(node: dict) -> dict[str, str]:
    raw = node.get("attributes", [])
    return dict(zip(raw[::2], raw[1::2]))


def _to_rect(node: dict, model: dict) -> ButtonRect:
    quad = (model.get("border") or model.get("content") or [])[:]
    xs, ys = quad[0::2], quad[1::2]
    label = (_attrs(node).get("class") or "").strip()
    return ButtonRect(
        x=float(min(xs)),
        y=float(min(ys)),
        width=float(max(xs) - min(xs)),
        height=float(max(ys) - min(ys)),
        label=f"{node.get('nodeName', '').lower()}.{label}".strip("."),
    )


async def _find(session: _Session, want_class: str) -> tuple[ButtonRect, int] | None:
    doc = await session.call("DOM.getDocument", {"depth": -1, "pierce": True})
    nodes: list[dict] = []
    _walk(doc["root"], nodes)

    candidates = [
        n
        for n in nodes
        if n.get("nodeName", "").lower() == PUBLISH_BTN_TAG
        and want_class in (_attrs(n).get("class") or "")
    ]
    if not candidates:
        return None

    # 同 class 可能有多个，取最靠下的那个（底部操作栏）
    best: tuple[ButtonRect, int] | None = None
    for node in candidates:
        node_id = node.get("nodeId")
        if not node_id:
            continue
        try:
            box = await session.call("DOM.getBoxModel", {"nodeId": node_id})
        except PublishButtonError:
            continue
        model = box.get("model")
        if not model:
            continue
        rect = _to_rect(node, model)
        if rect.width <= 0 or rect.height <= 0:
            continue
        if best is None or rect.y > best[0].y:
            best = (rect, node_id)
    return best


def find_publish_button(
    port: int = DEFAULT_PORT, *, want_class: str = PUBLISH_BTN_CLASS
) -> ButtonRect:
    """定位「发布」按钮。穿透 closed shadow root。"""

    async def run() -> ButtonRect:
        targets = _list_targets(port)
        page = _pick_publish_page(targets)
        if not page:
            raise PublishButtonError(
                "没有找到发布页（URL 含 /publish）。先跑一次预览填表。"
            )
        async with _Session(page["webSocketDebuggerUrl"]) as session:
            found = await _find(session, want_class)
            if not found:
                raise PublishButtonError(
                    f"未找到 class 含 {want_class!r} 的 {PUBLISH_BTN_TAG} 元素。"
                    "页面结构可能又变了。"
                )
            return found[0]

    return asyncio.run(run())


def click_publish(port: int = DEFAULT_PORT, *, settle_seconds: float = 5.0) -> dict:
    """点击「发布」按钮并返回结果摘要。

    真实发布是不可逆的外部动作，调用前请确认已获授权。
    """

    async def run() -> dict:
        targets = _list_targets(port)
        page = _pick_publish_page(targets)
        if not page:
            raise PublishButtonError("没有找到发布页")

        async with _Session(page["webSocketDebuggerUrl"]) as session:
            found = await _find(session, PUBLISH_BTN_CLASS)
            if not found:
                raise PublishButtonError("未找到发布按钮")
            rect, node_id = found
            cx, cy = rect.center

            for event_type in ("mousePressed", "mouseReleased"):
                await session.call(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event_type,
                        "x": cx,
                        "y": cy,
                        "button": "left",
                        "clickCount": 1,
                    },
                )
                await asyncio.sleep(0.05)

            await asyncio.sleep(settle_seconds)

            state = await session.call(
                "Runtime.evaluate",
                {
                    "expression": """
                    (() => {
                      const text = document.body ? document.body.innerText : '';
                      const links = [...document.querySelectorAll('a[href*="explore"]')]
                        .map(a => a.href);
                      return JSON.stringify({
                        url: location.href,
                        hasSuccessHint: /发布成功|已发布|发布完成/.test(text),
                        noteLinks: links.slice(0, 3),
                        snippet: text.slice(0, 160).replace(/\\n+/g, ' | ')
                      });
                    })()
                    """,
                    "returnByValue": True,
                },
            )
            return {
                "clicked_at": [round(cx), round(cy)],
                "button": str(rect),
                "node_id": node_id,
                "page_state": state.get("result", {}).get("value"),
            }

    return asyncio.run(run())


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="定位/点击小红书发布按钮")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument(
        "--click", action="store_true", help="真的点击（会真实发布！）"
    )
    args = ap.parse_args()

    if args.click:
        print("⚠ 即将真实点击发布按钮")
        print(json.dumps(click_publish(args.port), ensure_ascii=False, indent=2))
    else:
        print("按钮位置：", find_publish_button(args.port))
