"""小红书平台适配器。

平台动作与业务状态解耦：
* 填表 / 保存草稿 / 跳转后台 / 点击发布都在本适配器内；
* 是否允许正式账号发布由业务层的设置与用户授权决定；
* 任何结果不确定都不写“待确认发布”状态，只返回 needs_review。

草稿箱 DOM 属于平台高频变化的区域，适配器会给出明确的人工兜底提示。
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from ..logincheck import check as check_login
from ..logincheck import evaluate, list_targets, navigate, pick_page
from ..publish import (
    PublishError,
    draft_mismatch,
    get_current_draft,
    publish_note,
    publish_current_draft,
)

PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
TOPIC_SEARCH_URL = "https://www.xiaohongshu.com/search_result?keyword={keyword}&type=54&source=web_note_detail_r10"


class XhsPlatformError(RuntimeError):
    pass


class XhsPublishUncertain(XhsPlatformError):
    pass


@dataclass(slots=True)
class TopicCandidate:
    name: str
    views_raw: str = ""
    views_number: int | None = None
    source: str = ""
    fetched_at: str = ""


def parse_views(text: str) -> int | None:
    value = str(text or "").strip().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(万|亿)?", value)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    if unit == "万":
        number *= 10_000
    elif unit == "亿":
        number *= 100_000_000
    return int(number)


TOPIC_JS = r"""
(() => {
  const lines = (document.body ? document.body.innerText : '')
    .split('\n').map(s => s.trim()).filter(Boolean);
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.startsWith('#') || line.length < 2) continue;
    let views = '';
    for (let j = i + 1; j < Math.min(i + 5, lines.length); j++) {
      const m = lines[j].match(/([\d.,]+\s*(?:万|亿)?)\s*(?:次)?浏览/);
      if (m) { views = m[1].replace(/\s+/g, ''); break; }
    }
    if (views) out.push({name: line, views: views});
  }
  return JSON.stringify(out.slice(0, 60));
})()
"""


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _new_tab(port: int, url: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}",
        method="PUT",
    )
    with _opener().open(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _close_tab(port: int, target_id: str) -> None:
    try:
        with _opener().open(f"http://127.0.0.1:{port}/json/close/{target_id}", timeout=5):
            pass
    except OSError:
        pass


class XhsAdapter:
    def __init__(
        self,
        *,
        port: int = 9222,
        repo: Path | str | None = None,
        python: str | Path = "python",
        account: str | None = None,
        timeout: int = 600,
    ):
        self.port = port
        self.repo = Path(repo) if repo else Path(r"D:\Code\VSCode\BKLT365\XiaohongshuSkills")
        self.python = str(python)
        self.account = account
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        status = check_login(self.port)
        return {
            "reachable": status.reachable,
            "logged_in": status.logged_in,
            "creator_logged_in": status.creator_logged_in,
            "page_url": status.page_url,
            "nickname": status.nickname,
            "account_id": status.account_id,
            "detail": status.describe(),
        }

    def native_url(self) -> str:
        return PUBLISH_URL

    def fetch_topics(self, keywords: Sequence[str], *, max_candidates: int = 30) -> dict[str, Any]:
        """每次实时打开话题搜索页读取浏览量；全部失败时明确返回 ok=False。"""
        unique_keywords = [keyword.strip() for keyword in keywords if keyword and keyword.strip()][:3]
        if not unique_keywords:
            return {"ok": False, "topics": [], "error": "缺少可用于搜索的关键词"}
        candidates: dict[str, TopicCandidate] = {}
        errors: list[str] = []
        for keyword in unique_keywords:
            url = TOPIC_SEARCH_URL.format(keyword=urllib.parse.quote(keyword))
            target: dict | None = None
            try:
                target = _new_tab(self.port, url)
                time.sleep(2.0)
                deadline = time.monotonic() + 8
                raw = ""
                while time.monotonic() < deadline:
                    raw = evaluate(target["webSocketDebuggerUrl"], TOPIC_JS) or ""
                    if raw and raw != "[]":
                        break
                    time.sleep(0.5)
                items = json.loads(raw) if raw else []
                if not items:
                    errors.append(f"{keyword}: 没有读到话题卡片")
                    continue
                for item in items:
                    name = str(item.get("name") or "").strip()
                    if not name.startswith("#") or len(name) < 2:
                        continue
                    views_raw = str(item.get("views") or "").strip()
                    views = parse_views(views_raw)
                    current = candidates.get(name)
                    if current and (current.views_number or 0) >= (views or 0):
                        continue
                    candidates[name] = TopicCandidate(
                        name=name,
                        views_raw=views_raw,
                        views_number=views,
                        source=f"实时搜索：{keyword}",
                        fetched_at=time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{keyword}: {exc}")
            finally:
                if target and target.get("id"):
                    _close_tab(self.port, target["id"])
        if not candidates:
            return {
                "ok": False,
                "topics": [],
                "error": "实时话题浏览量获取失败；可人工选择话题后继续，但不能静默使用旧缓存。"
                + ("；" + "；".join(errors) if errors else ""),
            }
        topics = sorted(
            candidates.values(),
            key=lambda item: (item.views_number is not None, item.views_number or 0),
            reverse=True,
        )[:max_candidates]
        return {
            "ok": True,
            "topics": [asdict(topic) for topic in topics],
            "error": "；".join(errors) if errors else "",
        }

    def create_draft(
        self,
        *,
        title: str,
        content: str,
        images: Sequence[Path],
        expected_count: int,
    ) -> dict[str, Any]:
        if len(images) != expected_count:
            raise XhsPlatformError("图片数量与笔记不一致")
        if not images:
            raise XhsPlatformError("没有可上传的图片")
        health = self.health()
        if not health["reachable"] or not health["logged_in"]:
            raise XhsPlatformError("小红书创作者中心未登录或 CDP 不可达：" + health["detail"])
        if len(images) > 18:
            raise XhsPlatformError("单篇小红书图文最多 18 张图片，请拆分为多篇")
        try:
            result = publish_note(
                title=title,
                content=content,
                images=images,
                preview=True,
                headless=bool(self.account is None),
                account=self.account,
                repo=self.repo,
                python=self.python,
                timeout=self.timeout,
                save_draft=True,
                check_empty=True,
                port=self.port,
            )
        except PublishError as exc:
            raise XhsPlatformError(str(exc)) from exc
        if not result.ok:
            raise XhsPlatformError("小红书填表失败：" + result.tail())
        locator = {
            "title": title,
            "content_sha": _text_sha(content),
            "image_count": len(images),
            "created_at": time.time(),
            "publish_url": PUBLISH_URL,
        }
        return {
            "locator": locator,
            "image_count": len(images),
            "page_state": result.page_state,
            "detail": result.tail(5),
        }

    def open_native(self) -> str:
        """跳转创作者中心发布/草稿页，登录态复用项目 Chrome。"""
        return PUBLISH_URL

    def open_draft(self, locator: dict[str, Any]) -> dict[str, Any]:
        """在草稿箱中按标题打开最新草稿。

        平台 DOM 变化时返回可执行的人工兜底说明，而不是假装成功。
        """
        title = str(locator.get("title") or "").strip()
        if not title:
            raise XhsPlatformError("草稿定位缺少标题")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise XhsPlatformError("缺少 Playwright，无法自动打开草稿箱；请手动打开草稿") from exc

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}")
                contexts = browser.contexts
                if not contexts:
                    raise XhsPlatformError("Chrome 没有可用上下文")
                context = contexts[0]
                page = next(
                    (item for item in context.pages if "/publish" in item.url),
                    context.new_page(),
                )
                page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(1500)
                # 草稿箱入口在不同版本可能是按钮或文字链。
                for selector in (
                    "text=草稿箱",
                    "button:has-text('草稿箱')",
                    "[class*='draft']",
                ):
                    locator_obj = page.locator(selector)
                    if locator_obj.count():
                        locator_obj.first.click(timeout=8_000)
                        break
                else:
                    raise XhsPlatformError("未找到草稿箱入口")
                page.wait_for_timeout(1500)
                # 优先找包含标题的卡片，再点其中的“继续编辑”。
                card = page.locator("div").filter(has_text=title).filter(has_text="继续编辑")
                buttons = page.get_by_text("继续编辑", exact=False)
                if card.count():
                    card.last.click(timeout=8_000)
                elif buttons.count():
                    # 无法确认标题对应关系时，候选过多就不冒险。
                    if buttons.count() > 3:
                        raise XhsPlatformError(
                            "草稿箱中同名草稿较多，无法唯一定位；请手动打开目标草稿后回到 Web 点发布"
                        )
                    buttons.first.click(timeout=8_000)
                else:
                    raise XhsPlatformError("草稿箱中没有找到目标草稿；请手动打开后再发布")
                page.wait_for_timeout(2500)
                state = get_current_draft(self.port) or {}
                return {"ok": True, "state": state, "url": page.url}
        except XhsPlatformError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise XhsPlatformError(
                "自动打开草稿失败；请在小红书后台手动打开目标草稿，再回到 Web 点击发布。"
                f"（{exc}）"
            ) from exc

    def read_current_draft(self) -> dict[str, Any] | None:
        try:
            return get_current_draft(self.port)
        except PublishError as exc:
            raise XhsPlatformError(str(exc)) from exc

    def verify_current(self, *, title: str, content: str, image_count: int) -> dict[str, Any]:
        state = self.read_current_draft()
        if not state:
            raise XhsPlatformError("当前没有打开的小红书草稿")
        mismatch = draft_mismatch(state, title, content)
        if mismatch:
            raise XhsPlatformError(mismatch)
        actual_count = int(state.get("images") or 0)
        if actual_count != int(image_count):
            raise XhsPlatformError(f"当前草稿图片数为 {actual_count}，目标为 {image_count}")
        return state

    def publish_current(self, *, title: str, content: str, image_count: int) -> dict[str, Any]:
        state = self.verify_current(title=title, content=content, image_count=image_count)
        try:
            result = publish_current_draft(title=title, content=content, port=self.port)
        except PublishError as exc:
            raise XhsPlatformError(str(exc)) from exc
        return {**result, "verified_state": state}


def _text_sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_number(value: int | None) -> int | None:
    if value is None:
        return None
    return int(math.floor(value))
