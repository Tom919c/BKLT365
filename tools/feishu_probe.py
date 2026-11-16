"""用 CDP 打开页面并抓取网络请求 + 响应体，用于定位飞书内部接口。

    python tools/feishu_probe.py <url> --seconds 18 --filter "uestc-bklt" --bodies

会新开一个标签页（不动已有页面），抓完保留标签页以便继续排查。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def browser_ws(port: int) -> str:
    with _opener().open(f"http://127.0.0.1:{port}/json/version", timeout=5) as r:
        return json.loads(r.read().decode())["webSocketDebuggerUrl"]


def new_tab(port: int, url: str = "about:blank") -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}",
        method="PUT",
    )
    with _opener().open(req, timeout=10) as r:
        return json.loads(r.read().decode())


async def run(ws_url: str, url: str, seconds: float, pattern: re.Pattern, want_body: bool):
    records: list[dict] = []
    async with websockets.connect(ws_url, max_size=200 * 1024 * 1024) as ws:
        seq = 0

        async def send(method, params=None):
            nonlocal seq
            seq += 1
            await ws.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
            return seq

        await send("Network.enable")
        await send("Page.enable")
        await send("Page.navigate", {"url": url})

        watched: dict[str, dict] = {}
        deadline = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < deadline:
            remain = deadline - asyncio.get_event_loop().time()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, remain))
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            method = msg.get("method")
            if method == "Network.requestWillBeSent":
                p = msg["params"]
                u = p["request"]["url"]
                if pattern.search(u):
                    watched[p["requestId"]] = {
                        "url": u,
                        "method": p["request"]["method"],
                        "postData": (p["request"].get("postData") or "")[:800],
                    }
            elif method == "Network.loadingFinished":
                rid = msg["params"]["requestId"]
                if rid in watched:
                    rec = watched.pop(rid)
                    if want_body:
                        rec["body"] = await _get_body(ws, send, rid)
                    records.append(rec)
            elif method == "Network.loadingFailed":
                watched.pop(msg["params"]["requestId"], None)

    return records


async def _get_body(ws, send, request_id: str) -> str:
    nonlocal_seq = await send("Network.getResponseBody", {"requestId": request_id})
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=4)
        except asyncio.TimeoutError:
            return "<超时>"
        msg = json.loads(raw)
        if msg.get("id") == nonlocal_seq:
            result = msg.get("result") or {}
            body = result.get("body", "")
            if result.get("base64Encoded"):
                return "<base64>"
            return body[:4000]
        # 其他事件丢弃


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--seconds", type=float, default=18.0)
    ap.add_argument("--filter", default=r"uestc-bklt\.feishu\.cn")
    ap.add_argument("--bodies", action="store_true", help="抓响应体（较慢）")
    ap.add_argument("--grep", help="只显示响应体里含该字符串的请求")
    args = ap.parse_args()

    try:
        browser_ws(args.port)
    except OSError as exc:
        print(f"CDP 不可达（{args.port}）：{exc}", file=sys.stderr)
        return 2

    tab = new_tab(args.port)
    print(f"新标签页 {tab['id']}，加载 {args.seconds}s ...\n")
    records = asyncio.run(
        run(
            tab["webSocketDebuggerUrl"],
            args.url,
            args.seconds,
            re.compile(args.filter),
            args.bodies,
        )
    )

    shown = 0
    for rec in records:
        body = rec.get("body", "")
        if args.grep and args.grep not in body:
            continue
        shown += 1
        print(f"{rec['method']} {rec['url'][:140]}")
        if rec.get("postData"):
            print(f"  req : {rec['postData'][:300]}")
        if body:
            print(f"  resp: {body[:1200]}")
        print()

    print(f"共 {len(records)} 条命中，显示 {shown} 条")
    print(f"标签页：{tab['id']}（保留）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
