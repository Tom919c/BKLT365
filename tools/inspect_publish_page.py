"""读取小红书发布页的实际表单状态，用于验证「预览发布」真的填进去了。

    python tools/inspect_publish_page.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from niguang365 import logincheck as lc  # noqa: E402

PROBE_JS = r"""
(() => {
  const q = (s) => document.querySelector(s);
  const titleInput =
    q('div.d-input input') ||
    q('input[placeholder*="标题"]') ||
    q('input.d-text');
  const editor =
    q('div.tiptap.ProseMirror') ||
    q('div.ProseMirror[contenteditable="true"]') ||
    q('div.ql-editor');
  const imgs = [...document.querySelectorAll('img')]
    .filter((i) => i.width > 60 && i.height > 60);
  const btn = q('.publish-page-publish-btn button');
  return JSON.stringify({
    url: location.href,
    title: titleInput ? titleInput.value : null,
    content: editor ? editor.innerText : null,
    editorHtmlLen: editor ? editor.innerHTML.length : 0,
    editorHtmlHead: editor ? editor.innerHTML.slice(0, 300) : null,
    uploadedImgs: imgs.length,
    publishBtnText: btn ? btn.innerText.trim() : null,
    topicChips: [...document.querySelectorAll('[class*="topic"], [class*="hash-tag"]')]
      .map((e) => e.innerText.trim()).filter(Boolean).slice(0, 10),
    bodySnippet: (document.body ? document.body.innerText : '')
      .slice(0, 200).replace(/\n+/g, ' | ')
  });
})()
"""


def main() -> int:
    targets = lc.list_targets()
    pages = [t for t in targets if t.get("type") == "page"]
    print(f"共 {len(pages)} 个页面：")
    for t in pages:
        print(f"  {t.get('url', '')}")

    publish_pages = [t for t in pages if "/publish" in t.get("url", "")]
    if not publish_pages:
        print("\n没有发布页，先跑一次 publish（预览模式）")
        return 1

    page = publish_pages[0]
    print(f"\n检查发布页：{page['url']}\n")

    raw = lc.evaluate(page["webSocketDebuggerUrl"], PROBE_JS)
    if raw is None:
        print("探测返回空（页面可能还在加载，稍后重试）")
        return 2

    print(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
