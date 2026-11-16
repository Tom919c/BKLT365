"""123 云盘网页版文件夹上传。

通过 Playwright 连接项目现有的 CDP Chrome，不调用 123 云盘内部接口。
上传单位始终是**整个文件夹**；上传后回读云盘目录，按相对路径核验文件。

    python -m niguang365 pan123-login
    python -m niguang365 pan123-upload "D:\\path\\to\\folder"

登录态保存在 Chrome profile 中。登录过期或出现验证码时，本模块会停止，
不会绕过验证，也不会把网页上的提示当作上传成功。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .browser import ensure

PAN123_HOME = "https://yun.123pan.cn/"
PAN123_HOSTS = ("yun.123pan.cn", "user.123pan.cn")
DIRECTORY_INPUT = "input[webkitdirectory]"
DEFAULT_TIMEOUT_SECONDS = 1800


class Pan123Error(RuntimeError):
    """123 云盘上传或核验失败。"""


class Pan123LoginRequired(Pan123Error):
    """需要人工登录或完成验证。"""


@dataclass(frozen=True)
class FolderManifest:
    """本地文件夹的只读清单。"""

    path: Path
    files: tuple[Path, ...]
    total_bytes: int

    @property
    def file_count(self) -> int:
        return len(self.files)

    def relative_name(self, file: Path) -> str:
        return file.relative_to(self.path).as_posix()


@dataclass(frozen=True)
class UploadResult:
    folder: str
    file_count: int
    total_bytes: int
    uploaded: bool
    remote_url: str

    @property
    def already_present(self) -> bool:
        return not self.uploaded

    def describe(self) -> str:
        action = "上传并核验完成" if self.uploaded else "云盘已存在且核验一致，未重复上传"
        return (
            f"{action}：{self.folder}\n"
            f"文件数：{self.file_count}\n"
            f"本地总大小：{self.total_bytes / 1024 / 1024:.2f} MiB\n"
            f"云盘页面：{self.remote_url}"
        )


def inspect_folder(folder: str | Path) -> FolderManifest:
    """读取上传清单；空目录、非目录路径直接拒绝。"""
    path = Path(folder).expanduser().resolve()
    if not path.is_dir():
        raise Pan123Error(f"不是有效文件夹：{path}")
    files = tuple(sorted((p for p in path.rglob("*") if p.is_file()), key=lambda p: str(p)))
    if not files:
        raise Pan123Error(f"文件夹为空，不上传：{path}")
    total = sum(p.stat().st_size for p in files)
    return FolderManifest(path=path, files=files, total_bytes=total)


def direct_entries(folder: Path) -> tuple[str, ...]:
    """首层文件和子目录名，用于逐层回读云盘。"""
    return tuple(sorted(p.name for p in folder.iterdir()))


def missing_names(expected: tuple[str, ...] | list[str], actual: set[str]) -> list[str]:
    """计算缺失名称；独立出来便于离线测试。"""
    return [name for name in expected if name not in actual]


def _is_login_url(url: str) -> bool:
    return "user.123pan.cn" in url or "centerlogin" in url


def _connect(playwright, port: int):
    try:
        return playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{port}", timeout=20_000
        )
    except Exception as exc:  # noqa: BLE001
        raise Pan123Error(
            f"无法连接 CDP Chrome（端口 {port}）：{exc}\n"
            "请先运行：python -m niguang365 start-browser start"
        ) from exc


def _get_page(browser):
    contexts = browser.contexts
    if not contexts:
        raise Pan123Error("Chrome 没有可用的浏览器上下文")
    context = contexts[0]
    for page in context.pages:
        if any(host in page.url for host in PAN123_HOSTS):
            return page
    return context.new_page()


def _page_main_text(page) -> str:
    try:
        main = page.locator("main").first
        if main.count():
            return main.inner_text()
    except Exception:  # noqa: BLE001
        pass
    return page.locator("body").inner_text()


def _page_names(page) -> set[str]:
    """读取首层条目的完整文本，包含 CSS 省略前的 textContent 和 title。"""
    script = """
    (root) => {
      const names = new Set();
      const add = (value) => {
        if (value && value.trim()) names.add(value.trim());
      };
      const scope = root.querySelector('main') || root.body;
      if (!scope) return [];
      scope.querySelectorAll('*').forEach((el) => {
        if (el.children.length === 0) add(el.textContent);
        add(el.getAttribute('title'));
        add(el.getAttribute('data-name'));
        add(el.getAttribute('aria-label'));
      });
      return Array.from(names);
    }
    """
    try:
        return set(page.evaluate(script, page.locator("body").element_handle()))
    except Exception:  # noqa: BLE001
        text = _page_main_text(page)
        return {line.strip() for line in text.splitlines() if line.strip()}


def _check_page_failure(page) -> None:
    if _is_login_url(page.url):
        raise Pan123LoginRequired("123 云盘登录已失效，请重新登录后重试")
    text = _page_main_text(page)
    failure_markers = ("上传失败", "上传出错", "网络异常", "重新上传")
    if any(marker in text for marker in failure_markers):
        lines = [line for line in text.splitlines() if any(m in line for m in failure_markers)]
        raise Pan123Error("云盘报告上传失败：" + "；".join(lines[:5]))


def _wait_for_login(page, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if _is_login_url(page.url):
            raise Pan123LoginRequired(
                "123 云盘未登录。请先运行 `python -m niguang365 pan123-login` "
                "并在 Chrome 中扫码或完成验证"
            )
        try:
            if page.locator(DIRECTORY_INPUT).count():
                return
        except Exception:  # noqa: BLE001
            pass
        if time.monotonic() >= deadline:
            text = _page_main_text(page)
            if "扫码登录" in text or "验证码登录" in text:
                raise Pan123LoginRequired("123 云盘需要登录或完成页面验证")
            raise Pan123Error("云盘页面未就绪，未找到文件夹上传控件")
        page.wait_for_timeout(500)


def _set_local_files_native(page, selector: str, files: list[Path]) -> None:
    """通过本机 CDP 设置文件路径，避免 Playwright 远程 50 MiB 限制。"""
    session = page.context.new_cdp_session(page)
    try:
        session.send("DOM.enable")
        document = session.send("DOM.getDocument", {"depth": -1, "pierce": True})
        node = session.send(
            "DOM.querySelector",
            {"nodeId": document["root"]["nodeId"], "selector": selector},
        )
        if not node.get("nodeId"):
            raise Pan123Error(f"未找到文件上传控件：{selector}")
        session.send(
            "DOM.setFileInputFiles",
            {"nodeId": node["nodeId"], "files": [str(p) for p in files]},
        )
    except Pan123Error:
        raise
    except Exception as exc:  # noqa: BLE001
        raise Pan123Error(f"Chrome 原生文件提交失败：{exc}") from exc
    finally:
        try:
            session.detach()
        except Exception:  # noqa: BLE001
            pass


def _named_entry(page, name: str):
    main = page.locator("main").first
    entries = main.get_by_text(name, exact=True)
    count = entries.count()
    for index in range(count - 1, -1, -1):
        entry = entries.nth(index)
        try:
            if entry.is_visible():
                return entry
        except Exception:  # noqa: BLE001
            continue
    raise Pan123Error(f"云盘列表中找不到可打开的条目：{name}")


def _wait_for_file_list(page, timeout: float = 30) -> None:
    """等待文件列表首屏完成，避免把尚未加载误判为目录不存在。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if page.locator(".table-file-name").count():
                page.wait_for_timeout(500)
                return
            text = _page_main_text(page)
            if "拖拽" in text or "暂无数据" in text or "点击上传" in text:
                page.wait_for_timeout(500)
                return
        except Exception:  # noqa: BLE001
            pass
        if time.monotonic() >= deadline:
            raise Pan123Error("等待云盘文件列表加载超时")
        page.wait_for_timeout(500)


def _hide_transfer_banner(page) -> None:
    """只隐藏遮挡文件行的会员推广浮层，不点击开通或改变账号状态。"""
    page.evaluate(
        """
        document.querySelectorAll('a[href*="transfer_list_banner"]').forEach(a => {
          let el = a;
          while (el && !(typeof el.className === 'string' && el.className.includes('upload_'))) {
            el = el.parentElement;
          }
          if (el) el.style.display = 'none';
        });
        """
    )


def _open_named_entry(page, name: str) -> None:
    entry = _named_entry(page, name)
    before = tuple(page.locator(".table-file-name").all_inner_texts())
    try:
        _hide_transfer_banner(page)
        row = entry.locator(
            "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
            "' mfy-table-row ')][1]"
        )
        target = row.first if row.count() else entry
        target.dblclick(timeout=10_000)
    except Exception as exc:  # noqa: BLE001
        raise Pan123Error(f"无法打开云盘目录：{name}；{exc}") from exc

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        page.wait_for_timeout(300)
        current = tuple(page.locator(".table-file-name").all_inner_texts())
        if current != before:
            return
    raise Pan123Error(f"目录打开未生效：{name}；为防止上传到错误位置，已停止")


def _wait_entries(page, expected: tuple[str, ...], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_missing = list(expected)
    while True:
        _check_page_failure(page)
        actual = _page_names(page)
        last_missing = missing_names(expected, actual)
        if not last_missing:
            return
        if time.monotonic() >= deadline:
            sample = "、".join(last_missing[:5])
            suffix = "……" if len(last_missing) > 5 else ""
            raise Pan123Error(
                f"云盘回读核验缺少 {len(last_missing)} 项：{sample}{suffix}"
            )
        page.wait_for_timeout(1000)


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f}{unit}"
        value /= 1024
    return f"{value:.2f}TB"


def _remote_sizes(page) -> dict[str, str]:
    sizes: dict[str, str] = {}
    names = page.locator(".table-file-name")
    for i in range(names.count()):
        name_node = names.nth(i)
        name = (name_node.text_content() or "").strip()
        row = name_node.locator(
            "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
            "' editable-row ')][1]"
        )
        if name and row.count():
            lines = [line.strip() for line in row.first.inner_text().splitlines() if line.strip()]
            if lines:
                sizes[name] = lines[-1]
    return sizes


def _verify_direct_file_sizes(page, files: list[Path]) -> None:
    remote = _remote_sizes(page)
    mismatches = []
    for path in files:
        expected = _human_size(path.stat().st_size)
        actual = remote.get(path.name)
        if actual != expected:
            mismatches.append(f"{path.name}（本地 {expected}，云盘 {actual or '未知'}）")
    if mismatches:
        raise Pan123Error("云盘文件大小核验不一致：\n" + "\n".join(mismatches[:20]))


def _sync_remote_tree(
    page,
    local_dir: Path,
    timeout: float,
    log: Callable[[str], None],
    changes: list[bool],
) -> int:
    """同步当前目录：同名条目跳过，只补传缺失文件；返回已核验文件数。"""
    current_names = _page_names(page)
    direct_files = sorted((p for p in local_dir.iterdir() if p.is_file()), key=lambda p: p.name)
    direct_dirs = sorted((p for p in local_dir.iterdir() if p.is_dir()), key=lambda p: p.name)

    missing_files = [p for p in direct_files if p.name not in current_names]
    if missing_files:
        file_input = page.locator('input[type="file"][multiple]').first
        if not file_input.count():
            raise Pan123Error("未找到多文件上传控件，页面结构可能已变化")
        log(f"补传 {len(missing_files)} 个文件到当前云盘目录")
        try:
            _set_local_files_native(page, 'input[type="file"][multiple]', missing_files)
            changes.append(True)
        except Exception as exc:  # noqa: BLE001
            raise Pan123Error(f"批量文件提交给浏览器失败：{exc}") from exc

    for child in direct_dirs:
        if child.name in current_names:
            continue
        log(f"创建并同步缺失子目录：{child.name}")
        directory_input = page.locator(DIRECTORY_INPUT).first
        try:
            directory_input.set_input_files(str(child), timeout=30_000)
            changes.append(True)
        except Exception as exc:  # noqa: BLE001
            raise Pan123Error(f"子目录提交给浏览器失败：{child.name}；{exc}") from exc
        _wait_entries(page, (child.name,), timeout)

    _wait_entries(page, direct_entries(local_dir), timeout)
    _verify_direct_file_sizes(page, direct_files)
    verified = len(direct_files)
    for child in direct_dirs:
        log(f"核验子目录：{child.name}")
        _open_named_entry(page, child.name)
        try:
            verified += _sync_remote_tree(page, child, timeout, log, changes)
        finally:
            page.go_back(wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(500)
    return verified


def _open_remote_folder(page, name: str) -> None:
    _wait_entries(page, (name,), 30)
    _open_named_entry(page, name)


def open_login(port: int = 9222) -> str:
    """在项目 Chrome 中打开 123 云盘，供人工扫码。"""
    ensure(port)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _connect(p, port)
        page = _get_page(browser)
        page.goto(PAN123_HOME, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1500)
        if _is_login_url(page.url):
            return "123 云盘登录页已在项目 Chrome 中打开，请扫码或完成验证。"
        try:
            _wait_for_login(page, timeout=10)
            return "123 云盘当前已登录，可以直接运行 pan123-upload。"
        except Pan123Error:
            return "请检查项目 Chrome 中的 123 云盘登录或验证页面。"


def _create_folder(page, name: str, timeout: float = 20) -> None:
    """在 123 云盘当前目录创建年度目录。

    只在目标路径不存在时调用；页面结构变化时明确失败，不猜测点击。
    """
    deadline = time.monotonic() + timeout
    clicked = False
    for text in ("新建文件夹", "新建"):
        button = page.get_by_text(text, exact=True)
        if button.count():
            button.first.click(timeout=8_000)
            clicked = True
            break
    if not clicked:
        raise Pan123Error(
            f"云盘缺少目录 {name}，且未找到“新建文件夹”入口；请先在网页中创建年度根目录"
        )
    page.wait_for_timeout(800)
    candidates = page.locator(
        "input[placeholder*='文件夹'], input[placeholder*='名称'], input[type='text']"
    )
    if not candidates.count():
        raise Pan123Error(f"新建目录 {name} 时未找到名称输入框")
    candidates.last.fill(name)
    for label in ("确定", "创建", "保存"):
        button = page.get_by_text(label, exact=True)
        if button.count():
            button.first.click(timeout=8_000)
            page.wait_for_timeout(1200)
            if name in _page_names(page):
                return
            break
    raise Pan123Error(f"新建目录 {name} 未在云盘列表中确认出现")


def _enter_remote_path(page, root_path: str) -> str:
    """从“我的文件”根目录逐层进入配置的年度目录。"""
    parts = [part.strip() for part in str(root_path).replace("\\", "/").split("/") if part.strip()]
    if parts and parts[0] in {"我的文件", "全部文件"}:
        parts = parts[1:]
    for part in parts:
        _wait_for_file_list(page)
        if part not in _page_names(page):
            _create_folder(page, part)
            _wait_entries(page, (part,), 30)
        _open_named_entry(page, part)
        page.wait_for_timeout(700)
    return "/".join(parts)


def upload_folder_to_root(
    folder: str | Path,
    root_path: str,
    *,
    port: int = 9222,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    log: Callable[[str], None] = print,
) -> UploadResult:
    """先导航到配置的年度根目录，再整目录上传并核验。"""
    manifest = inspect_folder(folder)
    ensure(port)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _connect(p, port)
        page = _get_page(browser)
        page.goto(PAN123_HOME, wait_until="domcontentloaded", timeout=60_000)
        _wait_for_login(page)
        _wait_for_file_list(page)
        remote_root = _enter_remote_path(page, root_path)
        log(f"已进入云盘年度根目录：{remote_root}")

        folder_name = manifest.path.name
        changes: list[bool] = []
        directory_input = page.locator(DIRECTORY_INPUT).first
        if not directory_input.count():
            raise Pan123Error("未找到 123 云盘的文件夹上传控件，页面结构可能已变化")

        deadline = time.monotonic() + max(60, timeout)
        already_exists = folder_name in _page_names(page)
        if already_exists:
            log("发现同名周目录，将只补充缺失文件，已有同名文件不动")
        else:
            log(f"创建并上传整个周目录：{folder_name}")
            try:
                directory_input.set_input_files(str(manifest.path), timeout=30_000)
                changes.append(True)
            except Exception as exc:  # noqa: BLE001
                raise Pan123Error(f"文件夹提交给浏览器失败：{exc}") from exc
            while folder_name not in _page_names(page):
                _check_page_failure(page)
                if time.monotonic() >= deadline:
                    raise Pan123Error("等待云盘周目录出现超时；请检查传输任务后再重试")
                page.wait_for_timeout(1000)

        _open_named_entry(page, folder_name)
        remaining = max(60, int(deadline - time.monotonic()))
        count = _sync_remote_tree(page, manifest.path, remaining, log, changes)
        log(f"云盘文件名、大小与目录结构核验通过：{count} 个文件")
        return UploadResult(
            folder=f"{remote_root}/{folder_name}",
            file_count=count,
            total_bytes=manifest.total_bytes,
            uploaded=bool(changes),
            remote_url=page.url,
        )


def delete_remote_files(
    folder_name: str,
    root_path: str,
    names: list[str],
    *,
    port: int = 9222,
    timeout: int = 300,
    log: Callable[[str], None] = print,
) -> list[str]:
    """只删除明确列出的旧文件；返回已确认删除的名称。

    跨周改名后只允许调用本函数处理上一版“未入选/入选”旧文件，
    不允许按目录清空，也不允许通配删除。
    """
    if not names:
        return []
    ensure(port)
    from playwright.sync_api import sync_playwright

    deleted: list[str] = []
    with sync_playwright() as p:
        browser = _connect(p, port)
        page = _get_page(browser)
        page.goto(PAN123_HOME, wait_until="domcontentloaded", timeout=60_000)
        _wait_for_login(page)
        _wait_for_file_list(page)
        _enter_remote_path(page, root_path)
        if folder_name not in _page_names(page):
            raise Pan123Error(f"云盘年度目录中找不到周目录：{folder_name}")
        _open_named_entry(page, folder_name)
        page.wait_for_timeout(800)
        for name in names:
            entries = page.locator(".table-file-name").filter(has_text=name)
            exact = page.locator(".table-file-name").filter(
                has_text=__import__("re").compile(rf"^{__import__('re').escape(name)}$")
            )
            node = exact.first if exact.count() else entries.first
            if not node.count():
                log(f"旧文件已不存在，视为清理完成：{name}")
                deleted.append(name)
                continue
            row = node.locator(
                "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
                "' editable-row ')][1]"
            )
            target = row.first if row.count() else node
            target.hover()
            clicked = False
            for label in ("删除", "更多", "···"):
                button = target.get_by_text(label, exact=True)
                if not button.count():
                    button = target.locator(f"[aria-label*='{label}'], [title*='{label}']")
                if button.count():
                    button.first.click(timeout=8_000)
                    clicked = True
                    if label == "删除":
                        break
                    page.wait_for_timeout(500)
            if not clicked:
                raise Pan123Error(f"找不到旧文件 {name} 的删除入口，已停止自动清理")
            for label in ("确定", "删除"):
                confirm = page.get_by_text(label, exact=True)
                if confirm.count():
                    confirm.last.click(timeout=8_000)
                    break
            page.wait_for_timeout(700)
            if name not in _page_names(page):
                deleted.append(name)
            else:
                raise Pan123Error(f"删除后仍在列表中：{name}")
        log(f"已确认清理旧文件 {len(deleted)} 个")
        return deleted


def upload_folder(
    folder: str | Path,
    *,
    port: int = 9222,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    log: Callable[[str], None] = print,
) -> UploadResult:
    """上传整个文件夹并回读核验；重复执行不会盲目追加。"""
    manifest = inspect_folder(folder)
    log(
        f"本地清单：{manifest.path.name}，{manifest.file_count} 个文件，"
        f"{manifest.total_bytes / 1024 / 1024:.2f} MiB"
    )
    ensure(port)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _connect(p, port)
        page = _get_page(browser)
        page.goto(PAN123_HOME, wait_until="domcontentloaded", timeout=60_000)
        _wait_for_login(page)
        _wait_for_file_list(page)
        log("123 云盘登录状态正常，检查目标文件夹是否已存在")

        folder_name = manifest.path.name
        changes: list[bool] = []
        already_exists = folder_name in _page_names(page)
        directory_input = page.locator(DIRECTORY_INPUT).first
        if not directory_input.count():
            raise Pan123Error("未找到 123 云盘的文件夹上传控件，页面结构可能已变化")

        deadline = time.monotonic() + max(60, timeout)
        if already_exists:
            log("发现同名云盘文件夹，将只补充缺失文件，已有同名文件不动")
        else:
            log(f"创建并上传整个文件夹：{folder_name}")
            try:
                directory_input.set_input_files(str(manifest.path), timeout=30_000)
                changes.append(True)
            except Exception as exc:  # noqa: BLE001
                raise Pan123Error(f"文件夹提交给浏览器失败：{exc}") from exc
            log("浏览器已接收文件夹，等待云盘生成目录")
            while folder_name not in _page_names(page):
                _check_page_failure(page)
                if time.monotonic() >= deadline:
                    raise Pan123Error("等待云盘目标文件夹出现超时；请检查传输任务后再重试")
                page.wait_for_timeout(1000)

        _open_named_entry(page, folder_name)
        remaining = max(60, int(deadline - time.monotonic()))
        count = _sync_remote_tree(page, manifest.path, remaining, log, changes)
        log(f"云盘文件名、大小与目录结构核验通过：{count} 个文件")
        return UploadResult(
            folder=folder_name,
            file_count=count,
            total_bytes=manifest.total_bytes,
            uploaded=bool(changes),
            remote_url=page.url,
        )
