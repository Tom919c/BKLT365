"""启动 / 检测 CDP Chrome。

## 为什么需要这个模块

上游 ``chrome_launcher.py`` 用普通 ``subprocess.Popen`` 起 Chrome，起完自己就退出。
在 **WorkBuddy 的 Bash 工具**里，Chrome 会在命令结束时被回收，
表现为「报 ready 后端口 9222 无人监听」。

## 实测过的四种启动方式（2026-09-15）

| 方式 | 结果 |
|---|---|
| 普通 `subprocess.Popen` | ❌ 命令结束即被回收 |
| `Popen` + `DETACHED_PROCESS \\| CREATE_BREAKAWAY_FROM_JOB` | ❌ 同样被回收 |
| `os.startfile`（走 ShellExecute） | ❌ 同样被回收 |
| `schtasks`（任务计划程序） | ❌ 被沙箱安全策略拦截 |
| **Bash 工具 `run_in_background: true`** | ✅ **唯一可行** |

结论：**在 WorkBuddy 会话里，Chrome 必须用 `run_in_background: true` 启动。**
本模块的 ``start()`` 面向普通终端（工作室电脑），那里没有进程回收，可直接用。

## 在 WorkBuddy 里启动 Chrome

```
Bash 工具，run_in_background = true：
"/c/Program Files/Google/Chrome/Application/chrome.exe" \
  --remote-debugging-port=9222 \
  --user-data-dir="C:\\Users\\chenquan\\AppData\\Local\\Google\\Chrome\\XiaohongshuProfiles\\default" \
  --no-first-run --no-default-browser-check about:blank
```
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 9222
DEFAULT_PROFILE = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "XiaohongshuProfiles" / "default"

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

# Windows 进程创建标志
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

MANUAL_HINT = (
    "在 WorkBuddy 会话里请用 Bash 工具（run_in_background=true）启动 Chrome：\n"
    '  "/c/Program Files/Google/Chrome/Application/chrome.exe" \\\n'
    "    --remote-debugging-port=9222 \\\n"
    '    --user-data-dir="%LOCALAPPDATA%\\Google\\Chrome\\XiaohongshuProfiles\\default" \\\n'
    "    --no-first-run --no-default-browser-check about:blank\n"
    "在普通终端（如工作室电脑）可直接跑：python -m niguang365.browser start"
)


class BrowserError(RuntimeError):
    pass


@dataclass
class BrowserStatus:
    alive: bool
    port: int
    version: str = ""
    profile_dir: str = ""
    error: str = ""

    def describe(self) -> str:
        if self.alive:
            return f"CDP 可用（端口 {self.port}）：{self.version}"
        return f"CDP 不可用（端口 {self.port}）：{self.error}"


def find_chrome() -> str:
    """定位 chrome.exe。不依赖 PROGRAMFILES（Git Bash 不导出它）。"""
    for path in CHROME_CANDIDATES:
        if Path(path).is_file():
            return path
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            candidate = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if candidate.is_file():
                return str(candidate)
    raise BrowserError("找不到 chrome.exe，请确认已安装 Google Chrome")


def port_open(port: int = DEFAULT_PORT, timeout: float = 1.5) -> bool:
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def cdp_version(port: int = DEFAULT_PORT) -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=5) as resp:
        return json.loads(resp.read().decode()).get("Browser", "")


def status(port: int = DEFAULT_PORT) -> BrowserStatus:
    if not port_open(port):
        return BrowserStatus(alive=False, port=port, error="端口无监听")
    try:
        return BrowserStatus(
            alive=True, port=port, version=cdp_version(port), profile_dir=str(DEFAULT_PROFILE)
        )
    except Exception as exc:  # noqa: BLE001
        return BrowserStatus(alive=False, port=port, error=str(exc))


def _flags(with_breakaway: bool) -> int:
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    if with_breakaway:
        flags |= CREATE_BREAKAWAY_FROM_JOB
    return flags


def start(
    port: int = DEFAULT_PORT,
    *,
    headless: bool = False,
    profile_dir: str | Path | None = None,
    timeout: float = 25.0,
) -> BrowserStatus:
    """启动带调试端口的 Chrome。已在跑则直接返回。"""
    current = status(port)
    if current.alive:
        return current

    chrome = find_chrome()
    profile = Path(profile_dir or DEFAULT_PROFILE)
    profile.mkdir(parents=True, exist_ok=True)

    cmd = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-background-media-suspend",
    ]
    if headless:
        cmd.append("--headless=new")
    cmd.append("about:blank")

    last_error = ""
    for with_breakaway in (True, False):
        try:
            subprocess.Popen(
                cmd,
                creationflags=_flags(with_breakaway),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            last_error = str(exc)
            continue

        deadline = time.time() + timeout
        while time.time() < deadline:
            if port_open(port):
                time.sleep(0.4)
                return status(port)
            time.sleep(0.5)

        last_error = f"{timeout:.0f}s 内端口 {port} 未就绪"

    raise BrowserError(
        f"Chrome 启动失败：{last_error}\n"
        "常见原因：同 profile 已有实例在跑（新进程只转发 URL 后退出）。\n"
        f"请手动关闭使用 {profile} 的 Chrome 后重试。\n\n"
        + MANUAL_HINT
    )


def ensure(port: int = DEFAULT_PORT, **kwargs) -> BrowserStatus:
    """确保 CDP 可用，不可用就尝试启动。

    注意：在 WorkBuddy 会话里 ``start()`` 起的 Chrome 会被回收，
    此时会抛 ``BrowserError``，错误信息里带手动启动指引。
    """
    current = status(port)
    if current.alive:
        return current
    return start(port, **kwargs)


def require(port: int = DEFAULT_PORT) -> BrowserStatus:
    """CDP 不可用就抛带指引的错误，供命令入口统一调用。"""
    current = status(port)
    if not current.alive:
        raise BrowserError(
            f"CDP Chrome 未运行（端口 {port} 无监听）。\n\n" + MANUAL_HINT
        )
    return current


def stop(port: int = DEFAULT_PORT) -> bool:
    """通过 CDP 关闭浏览器。"""
    if not port_open(port):
        return False
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=5) as resp:
            ws_url = json.loads(resp.read().decode()).get("webSocketDebuggerUrl")
        if not ws_url:
            return False
        import asyncio

        import websockets

        async def close():
            async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                try:
                    await asyncio.wait_for(ws.recv(), timeout=3)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    pass

        asyncio.run(close())
        time.sleep(1.0)
        return not port_open(port)
    except Exception:  # noqa: BLE001
        return False


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="CDP Chrome 启停")
    ap.add_argument("action", choices=("status", "start", "stop", "restart"))
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    try:
        if args.action == "status":
            print(status(args.port).describe())
            return 0 if status(args.port).alive else 1

        if args.action == "start":
            print(start(args.port, headless=args.headless).describe())
            return 0

        if args.action == "stop":
            print("已关闭" if stop(args.port) else "端口本来就没监听")
            return 0

        if args.action == "restart":
            stop(args.port)
            print(start(args.port, headless=args.headless).describe())
            return 0
    except BrowserError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
