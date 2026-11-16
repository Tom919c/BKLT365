"""登录态排查工具。

上游的 ``check-login`` 只看 URL 里有没有 "login"，creator 首页不跳转时
会**误报已登录**。本工具改查真实凭证（web_session cookie + 鉴权接口）。

    python tools/probe_login.py                # 检测
    python tools/probe_login.py --open-login   # 导航到登录页并检测
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from niguang365 import logincheck  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument(
        "--open-login",
        action="store_true",
        help="先把 Chrome 导航到创作者平台登录页，再检测",
    )
    args = ap.parse_args()

    status = (
        logincheck.open_login_page(args.port)
        if args.open_login
        else logincheck.check(args.port)
    )

    print(status.describe())

    if not status.reachable:
        print()
        print("Chrome 没在跑（或调试端口未开）。启动方式：", file=sys.stderr)
        print(
            '  chrome.exe --remote-debugging-port=9222 '
            '--user-data-dir="%LOCALAPPDATA%\\Google\\Chrome\\XiaohongshuProfiles\\default"',
            file=sys.stderr,
        )
        print("注意：必须用后台方式启动，否则进程会被回收。", file=sys.stderr)
        return 2

    if not status.logged_in:
        print()
        print("请在 Chrome 窗口里扫码登录，然后重新检测。")

    return 0 if status.logged_in else 1


if __name__ == "__main__":
    raise SystemExit(main())
