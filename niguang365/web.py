"""逆光365 手机选图 Web MVP。

运行：``python -m niguang365.web --host 127.0.0.1 --port 8501``。
首次运行先执行 ``python -m niguang365.web --init-auth`` 生成本地登录配置。

安全边界：飞书只读；原图只读；预览与发布严格分离，发布必须由主编在页面
二次确认，后端点击前再次核对草稿标题、正文和标签。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .cli import load_config
from .collect import (
    CollectError,
    candidate_id,
    save_web_selection,
    scan_inbox,
)
from .compose import ComposeError, build_content
from .feishu import FeishuError, OfficialFeishuBase
from .naming import is_image
from .publish import (
    PublishError,
    draft_mismatch,
    get_current_draft,
    publish_current_draft,
    publish_note,
)

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
LOCAL_AUTH_PATH = PACKAGE_DIR / "web.local.json"
PREVIEW_FILENAME = ".niguang365-preview.json"
PUBLISHED_FILENAME = ".niguang365-published.json"
SESSION_COOKIE = "niguang365_session"
PASSWORD_ROUNDS = 240_000
SESSION_DAYS = 7


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class SelectionRequest(BaseModel):
    date: str
    selected: list[str] = Field(default_factory=list, max_length=500)


class PlatformPreviewRequest(BaseModel):
    date: str
    id: str = Field(min_length=1, max_length=1000)


class PlatformPublishRequest(PlatformPreviewRequest):
    confirmed: bool = False


def _password_hash(password: str, salt_hex: str, rounds: int = PASSWORD_ROUNDS) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), rounds
    ).hex()


def make_auth_config(password: str) -> dict[str, Any]:
    """生成不包含明文密码的 Web 配置，供初始化和测试使用。"""
    salt = secrets.token_hex(16)
    return {
        "password_salt": salt,
        "password_hash": _password_hash(password, salt),
        "session_secret": secrets.token_hex(32),
        "session_days": SESSION_DAYS,
        "preview_headless": True,
    }


def initialize_auth(path: Path = LOCAL_AUTH_PATH, password: str | None = None):
    """首次生成本地登录配置；已存在时不覆盖。"""
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8")), None, False
    initial_password = password or secrets.token_urlsafe(12)
    config = make_auth_config(initial_password)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)
    return config, initial_password, True


def _verify_password(password: str, config: dict[str, Any]) -> bool:
    expected = str(config.get("password_hash") or "")
    salt = str(config.get("password_salt") or "")
    if not expected or not salt:
        return False
    actual = _password_hash(password, salt)
    return hmac.compare_digest(actual, expected)


def _parse_day(value: str) -> date:
    if not re.fullmatch(r"\d{8}", value or ""):
        raise HTTPException(status_code=400, detail="日期格式必须是 YYYYMMDD")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="日期不存在") from exc


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def create_app(config: dict[str, Any] | None = None) -> FastAPI:
    cfg = dict(config or load_config())
    if config is None and LOCAL_AUTH_PATH.is_file():
        local_auth = json.loads(LOCAL_AUTH_PATH.read_text(encoding="utf-8"))
        cfg["web"] = {**(cfg.get("web") or {}), **local_auth}
    web_cfg = dict(cfg.get("web") or {})
    required = ("password_salt", "password_hash", "session_secret")
    if not all(web_cfg.get(key) for key in required):
        raise RuntimeError(
            "Web 登录尚未配置，请先运行 python -m niguang365.web --init-auth"
        )

    app = FastAPI(
        title="逆光365 轮值选图",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    operation_lock = threading.Lock()
    failed_logins: dict[str, dict[str, float]] = {}
    secret = str(web_cfg["session_secret"]).encode("ascii")
    session_days = int(web_cfg.get("session_days") or SESSION_DAYS)

    def issue_session() -> tuple[str, int]:
        expires = int(time.time()) + session_days * 86400
        signature = hmac.new(
            secret, str(expires).encode("ascii"), hashlib.sha256
        ).hexdigest()
        return f"{expires}.{signature}", expires

    def authenticated(request: Request) -> bool:
        token = request.cookies.get(SESSION_COOKIE) or ""
        try:
            expires_text, signature = token.split(".", 1)
            expires = int(expires_text)
        except (ValueError, TypeError):
            return False
        expected = hmac.new(
            secret, expires_text.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return expires > time.time() and hmac.compare_digest(signature, expected)

    def require_auth(request: Request) -> None:
        if not authenticated(request):
            raise HTTPException(status_code=401, detail="请先登录")

    def scan_root(on_date: date) -> Path:
        inbox = Path(cfg["root"]) / "收件箱"
        day_dir = inbox / on_date.strftime("%Y%m%d")
        return day_dir if day_dir.is_dir() else inbox

    def scan(on_date: date):
        return scan_inbox(cfg["root"], on_date=on_date, roster_path=cfg.get("roster"))

    def serialize_candidate(on_date: date, root: Path, cand) -> dict:
        cid = candidate_id(cand.path, root)
        return {
            "id": cid,
            "filename": cand.path.name,
            "photographer": cand.info.photographer,
            "title": cand.info.title,
            "selected": cand.selected,
            "handle": cand.handle,
            "quote": cand.quote,
            "problems": cand.problems,
            "image_url": f"/media/{on_date:%Y%m%d}/{quote(cid, safe='/')}",
        }

    def published_path(on_date: date) -> Path:
        return Path(cfg["root"]) / "收件箱" / on_date.strftime("%Y%m%d") / PUBLISHED_FILENAME

    def read_published(on_date: date) -> dict:
        path = published_path(on_date)
        if not path.is_file():
            return {
                "version": 1,
                "date": on_date.strftime("%Y%m%d"),
                "items": {},
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "version": 1,
                "date": on_date.strftime("%Y%m%d"),
                "items": {},
            }
        if not isinstance(payload.get("items"), dict):
            payload["items"] = {}
        return payload

    def save_published(on_date: date, item_id: str, entry: dict) -> None:
        payload = read_published(on_date)
        payload.setdefault("items", {})[item_id] = entry
        payload["version"] = 1
        payload["date"] = on_date.strftime("%Y%m%d")
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json_atomic(published_path(on_date), payload)

    def build_previews(on_date: date, root: Path, result) -> list[dict]:
        previews = []
        published = read_published(on_date).get("items", {})
        for cand in result.selected:
            item = serialize_candidate(on_date, root, cand)
            cid = item["id"]
            entry = published.get(cid) if isinstance(published.get(cid), dict) else None
            item["published"] = bool(entry)
            item["published_at"] = (entry or {}).get("published_at")
            item["note_url"] = (entry or {}).get("note_url")
            try:
                composed = build_content(
                    cand.info,
                    quote=cand.quote,
                    xhs_handle=cand.handle,
                    tags=cfg["tags"],
                )
                item.update(
                    {
                        "ok": True,
                        "post_title": composed.title,
                        "content": composed.content,
                        "tags": composed.tags,
                        "warnings": composed.warnings,
                    }
                )
            except ComposeError as exc:
                item.update(
                    {
                        "ok": False,
                        "post_title": "",
                        "content": "",
                        "tags": [],
                        "warnings": [],
                        "error": str(exc),
                    }
                )
            previews.append(item)
        return previews

    def candidates_payload(on_date: date) -> dict:
        root = scan_root(on_date)
        result = scan(on_date)
        return {
            "date": on_date.strftime("%Y%m%d"),
            "candidates": [serialize_candidate(on_date, root, c) for c in result.candidates],
            "unparsed": [
                {"filename": path.name, "reason": reason}
                for path, reason in result.unparsed
            ],
        }

    def available_dates() -> list[dict]:
        inbox = Path(cfg["root"]) / "收件箱"
        found: list[date] = []
        if inbox.is_dir():
            for path in inbox.iterdir():
                if path.is_dir() and re.fullmatch(r"\d{8}", path.name):
                    try:
                        found.append(datetime.strptime(path.name, "%Y%m%d").date())
                    except ValueError:
                        continue
        if not found:
            found.append(date.today())
        output = []
        for on_date in sorted(set(found), reverse=True):
            try:
                payload = candidates_payload(on_date)
                output.append(
                    {
                        "date": payload["date"],
                        "count": len(payload["candidates"]),
                        "selected": sum(c["selected"] for c in payload["candidates"]),
                    }
                )
            except CollectError:
                continue
        return output

    def locked():
        if not operation_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="另一个操作正在执行，请稍后刷新")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "niguang365-web"}

    @app.get("/api/session")
    def session(request: Request):
        return {"authenticated": authenticated(request)}

    @app.post("/api/login")
    def login(payload: LoginRequest, request: Request):
        client = request.headers.get("cf-connecting-ip") or (
            request.client.host if request.client else "unknown"
        )
        state = failed_logins.get(client, {"count": 0, "until": 0})
        if state["until"] > time.time():
            raise HTTPException(status_code=429, detail="尝试次数过多，请 5 分钟后再试")
        if not _verify_password(payload.password, web_cfg):
            state["count"] += 1
            if state["count"] >= 5:
                state["until"] = time.time() + 300
                state["count"] = 0
            failed_logins[client] = state
            raise HTTPException(status_code=401, detail="密码不正确")
        failed_logins.pop(client, None)
        token, _ = issue_session()
        response = JSONResponse({"ok": True})
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=session_days * 86400,
            httponly=True,
            secure=(forwarded == "https" or request.url.scheme == "https"),
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/api/logout")
    def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/dates")
    def dates(request: Request):
        require_auth(request)
        return {"dates": available_dates()}

    @app.get("/api/candidates")
    def candidates(request: Request, date_str: str | None = Query(None, alias="date")):
        require_auth(request)
        if date_str:
            on_date = _parse_day(date_str)
        else:
            dates_list = available_dates()
            if not dates_list:
                raise HTTPException(status_code=404, detail="暂无投稿日期")
            on_date = _parse_day(dates_list[0]["date"])
        try:
            return candidates_payload(on_date)
        except CollectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/previews")
    def previews(request: Request, date_str: str = Query(alias="date")):
        require_auth(request)
        on_date = _parse_day(date_str)
        root = scan_root(on_date)
        return {
            "date": on_date.strftime("%Y%m%d"),
            "previews": build_previews(on_date, root, scan(on_date)),
        }

    @app.get("/media/{day}/{relative:path}")
    def media(day: str, relative: str, request: Request):
        require_auth(request)
        on_date = _parse_day(day)
        root = scan_root(on_date).resolve()
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="图片不存在") from exc
        if not path.is_file() or not is_image(path):
            raise HTTPException(status_code=404, detail="图片不存在")
        return FileResponse(
            path,
            content_disposition_type="inline",
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.post("/api/sync")
    def sync_feishu(request: Request):
        require_auth(request)
        fc = cfg.get("feishu") or {}
        if not fc.get("app_token") or not fc.get("table_id"):
            raise HTTPException(status_code=400, detail="飞书配置不完整")
        locked()
        try:
            base = OfficialFeishuBase(
                fc["app_token"],
                fc["table_id"],
                cli_path=fc.get("cli_path") or "lark-cli",
                view_id=fc.get("view_id"),
                host=fc.get("host") or "https://uestc-bklt.feishu.cn",
                port=int(fc.get("cdp_port") or 9222),
                wiki_token=fc.get("wiki_token"),
            )
            report = base.sync(
                cfg["root"],
                rename=fc.get("rename") or "keep",
                name_field_id=fc.get("name_field_id"),
                title_field_id=fc.get("title_field_id"),
            )
            return {
                "ok": True,
                "downloaded": len(report.downloaded),
                "files": [Path(path).name for _, path in report.downloaded],
                "skipped": [{"filename": name, "reason": why} for name, why in report.skipped],
            }
        except FeishuError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            operation_lock.release()

    @app.post("/api/selection")
    def submit_selection(payload: SelectionRequest, request: Request):
        require_auth(request)
        on_date = _parse_day(payload.date)
        locked()
        try:
            root = scan_root(on_date)
            before = scan(on_date)
            by_id = {candidate_id(c.path, root): c for c in before.candidates}
            selected = list(dict.fromkeys(payload.selected))
            unknown = [cid for cid in selected if cid not in by_id]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail="有文件已变化，请刷新后重选：" + "、".join(unknown),
                )
            selection_path = save_web_selection(
                cfg["root"], on_date=on_date, selected=selected
            )
            after = scan(on_date)
            preview_items = build_previews(on_date, root, after)
            preview_path = root / PREVIEW_FILENAME
            _write_json_atomic(
                preview_path,
                {
                    "version": 1,
                    "date": on_date.strftime("%Y%m%d"),
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "platform": "xiaohongshu",
                    "published": False,
                    "items": preview_items,
                },
            )
            return {
                "ok": True,
                "selected_count": len(after.selected),
                "selection_file": selection_path.name,
                "preview_file": preview_path.name,
                "previews": preview_items,
                "unparsed": [
                    {"filename": path.name, "reason": reason}
                    for path, reason in after.unparsed
                ],
            }
        except CollectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            operation_lock.release()

    @app.post("/api/platform-preview")
    def platform_preview(payload: PlatformPreviewRequest, request: Request):
        """只把一篇笔记填入小红书后台；固定预览模式，不提供发布参数。"""
        require_auth(request)
        on_date = _parse_day(payload.date)
        locked()
        try:
            root = scan_root(on_date)
            result = scan(on_date)
            cand = next(
                (c for c in result.candidates if candidate_id(c.path, root) == payload.id),
                None,
            )
            if cand is None:
                raise HTTPException(status_code=404, detail="图片不存在，请刷新")
            if not cand.selected:
                raise HTTPException(status_code=400, detail="请先把这张图片保存为入选")
            if read_published(on_date)["items"].get(payload.id):
                raise HTTPException(
                    status_code=409,
                    detail="这篇已经发布过，不能再次填入草稿。",
                )
            composed = build_content(
                cand.info,
                quote=cand.quote,
                xhs_handle=cand.handle,
                tags=cfg["tags"],
            )
            result = publish_note(
                title=composed.title,
                content=composed.content,
                image=cand.path,
                preview=True,
                headless=bool(web_cfg.get("preview_headless", True)),
                account=cfg.get("account"),
                post_time=cfg.get("post_time"),
                repo=cfg["repo"],
                python=cfg["python"],
            )
            response = {
                "ok": result.ok,
                "preview": True,
                "published": False,
                "returncode": result.returncode,
                "tail": result.tail(12),
            }
            return response if result.ok else JSONResponse(response, status_code=502)
        except (ComposeError, PublishError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            operation_lock.release()

    @app.get("/api/platform-draft")
    def platform_draft(date: str, id: str, request: Request):
        """只读检查当前小红书草稿是否就是这篇稿件。"""
        require_auth(request)
        on_date = _parse_day(date)
        locked()
        try:
            root = scan_root(on_date)
            result = scan(on_date)
            cand = next(
                (c for c in result.candidates if candidate_id(c.path, root) == id),
                None,
            )
            if cand is None:
                raise HTTPException(status_code=404, detail="图片不存在，请刷新")
            if not cand.selected:
                raise HTTPException(status_code=400, detail="这张图片不是入选状态")
            if read_published(on_date)["items"].get(id):
                return {
                    "exists": True,
                    "matches": False,
                    "published": True,
                    "message": "这篇已经发布过，不能重复发布。",
                }
            composed = build_content(
                cand.info,
                quote=cand.quote,
                xhs_handle=cand.handle,
                tags=cfg["tags"],
            )
            try:
                state = get_current_draft(int(cfg.get("publish_port") or 9222))
            except PublishError as exc:
                return {
                    "exists": False,
                    "matches": False,
                    "published": False,
                    "message": str(exc),
                }
            if not state:
                return {
                    "exists": False,
                    "matches": False,
                    "published": False,
                    "message": "当前没有已打开的小红书发布页，请先填入后台预览。",
                }
            mismatch = draft_mismatch(state, composed.title, composed.content)
            return {
                "exists": True,
                "matches": not mismatch,
                "published": False,
                "title": state.get("title") or "",
                "images": int(state.get("images") or 0),
                "message": mismatch or "后台草稿与这篇稿件一致，可以确认发布。",
            }
        except HTTPException:
            raise
        except (ComposeError, PublishError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            operation_lock.release()

    @app.post("/api/platform-publish")
    def platform_publish(payload: PlatformPublishRequest, request: Request):
        """发布当前已核对的小红书草稿；必须由主编显式确认。"""
        require_auth(request)
        if not payload.confirmed:
            raise HTTPException(
                status_code=400, detail="发布前必须在页面确认预览内容。"
            )
        on_date = _parse_day(payload.date)
        locked()
        try:
            root = scan_root(on_date)
            result = scan(on_date)
            cand = next(
                (c for c in result.candidates if candidate_id(c.path, root) == payload.id),
                None,
            )
            if cand is None:
                raise HTTPException(status_code=404, detail="图片不存在，请刷新")
            if not cand.selected:
                raise HTTPException(status_code=400, detail="这张图片不是入选状态")
            if read_published(on_date)["items"].get(payload.id):
                raise HTTPException(status_code=409, detail="这篇已经发布过，不能重复发布。")

            composed = build_content(
                cand.info,
                quote=cand.quote,
                xhs_handle=cand.handle,
                tags=cfg["tags"],
            )
            port = int(cfg.get("publish_port") or 9222)
            result = publish_current_draft(
                title=composed.title,
                content=composed.content,
                port=port,
            )
            page_state = result.get("page_state")
            if isinstance(page_state, str):
                try:
                    page_state = json.loads(page_state)
                except json.JSONDecodeError:
                    page_state = None
            note_url = None
            page_url = None
            if isinstance(page_state, dict):
                links = page_state.get("noteLinks") or []
                note_url = str(links[0]) if links else None
                page_url = str(page_state.get("url") or "") or None

            if result.get("published"):
                save_published(
                    on_date,
                    payload.id,
                    {
                        "title": composed.title,
                        "published_at": datetime.now().isoformat(timespec="seconds"),
                        "note_url": note_url,
                        "page_url": page_url,
                    },
                )
                return {
                    "ok": True,
                    "clicked": True,
                    "published": True,
                    "needs_review": False,
                    "note_url": note_url,
                    "page_url": page_url,
                    "message": result.get("message"),
                    "page_state": page_state,
                }

            body = {
                "ok": False,
                "clicked": result.get("clicked"),
                "published": False,
                "needs_review": bool(result.get("needs_review")),
                "note_url": note_url,
                "page_url": page_url,
                "message": result.get("message"),
                "page_state": page_state,
            }
            if result.get("clicked") is False:
                raise HTTPException(status_code=502, detail=result.get("message") or "点击发布失败")
            return JSONResponse(body, status_code=202)
        except (ComposeError, PublishError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            operation_lock.release()

    return app



def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="逆光365 手机选图 Web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--init-auth", action="store_true", help="生成本地登录配置")
    args = parser.parse_args()

    if args.init_auth:
        _, password, created = initialize_auth()
        if created:
            print(f"已生成 {LOCAL_AUTH_PATH}")
            print(f"初始登录密码：{password}")
            print("请妥善保存；此文件包含哈希和会话密钥，不要提交到 Git。")
        else:
            print(f"登录配置已存在，未覆盖：{LOCAL_AUTH_PATH}")
        return 0

    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
