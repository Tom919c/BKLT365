"""FastAPI 新主入口：移动端工作台、组稿、草稿、发布、上传与设置。"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .core.auth import AuthError, AuthManager, SESSION_COOKIE
from .core.cloud_adapter import Pan123Adapter
from .core.config import APP_ROOT, AppConfig, load_config
from .core.feishu_adapter import FeishuAdapter
from .core.scheduler import UploadScheduler
from .core.service import Service, ServiceError
from .core.xhs_adapter import XhsAdapter

WEBUI_DIR = APP_ROOT / "webui"
AUTH_PATH = APP_ROOT / "web.local.json"


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class SyncRequest(BaseModel):
    week: str | None = None


class SelectRequest(BaseModel):
    publish_date: str


class GroupPatch(BaseModel):
    title: str | None = None
    quote: str | None = None


class ReorderRequest(BaseModel):
    photo_ids: list[str]


class NoteCreateRequest(BaseModel):
    group_ids: list[str]
    title: str | None = None
    publish_date: str
    day: str | None = None
    dynamic_topics: list[str] = Field(default_factory=list)


class NotePatch(BaseModel):
    title: str | None = None
    content: str | None = None
    day: str | None = None


class TopicsRequest(BaseModel):
    names: list[str]


class PublishRequest(BaseModel):
    confirmed: bool = False


class UploadRequest(BaseModel):
    allow_current: bool = False


class PasswordRequest(BaseModel):
    current: str
    new: str


def _create_adapters(config: AppConfig) -> tuple[Any, Any, Any, str]:
    error = ""
    feishu = None
    if config.feishu:
        try:
            feishu = FeishuAdapter(
                config.feishu,
                config.staging_root,
                cli_path=config.feishu.cli_path,
            )
        except Exception as exc:  # noqa: BLE001
            error = f"飞书适配器未就绪：{exc}"
    xhs = XhsAdapter(
        port=config.cdp_port,
        repo=config.repo_path,
        python=config.python_path,
        account=config.account,
    )
    cloud = Pan123Adapter(port=config.cdp_port, timeout=config.pan123_timeout)
    return feishu, xhs, cloud, error


def create_app(
    config: AppConfig | None = None,
    *,
    service: Service | None = None,
    auth: AuthManager | None = None,
    testing: bool = False,
) -> FastAPI:
    config = config or load_config()
    if service is None:
        feishu, xhs, cloud, adapter_error = _create_adapters(config)
        service = Service(config, feishu=feishu, xhs=xhs, cloud=cloud, topics=xhs)
        service.adapter_error = adapter_error  # type: ignore[attr-defined]
    if auth is None:
        auth = AuthManager(AUTH_PATH, service.db)
    scheduler = UploadScheduler(service)
    service.scheduler = scheduler  # type: ignore[attr-defined]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if config.scheduler_enabled and not testing:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(
        title="逆光365",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config
    app.state.service = service
    app.state.auth = auth
    app.state.scheduler = scheduler

    @app.middleware("http")
    async def ui_no_cache(request: Request, call_next):
        response = await call_next(request)
        if request.url.path in {"/", "/index.html", "/app.js", "/styles.css"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        return JSONResponse(
            status_code=getattr(exc, "status_code", 400),
            content={"ok": False, "message": str(exc)},
        )

    @app.exception_handler(AuthError)
    async def auth_error_handler(request: Request, exc: AuthError):
        return JSONResponse(status_code=401, content={"ok": False, "message": str(exc)})

    def current_session(request: Request) -> dict[str, Any]:
        token = request.cookies.get(SESSION_COOKIE)
        session = auth.authenticate(token)
        if not session:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "session_invalid",
                    "message": "登录已失效；如已在另一台设备登录，请重新登录。",
                },
            )
        return session

    def _set_cookie(response: Response, request: Request, token: str, expires_at: str) -> None:
        secure = (
            request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
            or request.url.scheme == "https"
        )
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=secure,
            samesite="lax",
            max_age=auth.session_days * 86400,
            path="/",
        )

    @app.get("/api/health")
    def health():
        return {"ok": True, "name": "逆光365"}

    @app.get("/api/session")
    def session_state(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        session = auth.authenticate(token)
        if not session:
            return {"authenticated": False}
        return {
            "authenticated": True,
            "expires_at": session["expires_at"],
            "last_activity": session["last_activity"],
        }

    @app.post("/api/login")
    def login(payload: LoginRequest, request: Request):
        user_agent = request.headers.get("user-agent", "")
        token, expires = auth.login(payload.password, user_agent=user_agent)
        response = JSONResponse({"ok": True, "expires_at": expires})
        _set_cookie(response, request, token, expires)
        service.db.audit("editor", "login", "", {"user_agent": user_agent[:120]})
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        auth.logout(request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/dashboard")
    def dashboard(week: str | None = None, _: dict = Depends(current_session)):
        return service.dashboard(week)

    @app.get("/api/weeks")
    def weeks(_: dict = Depends(current_session)):
        return {"weeks": [service._week_payload(item) for item in service.repo.list_weeks()]}

    @app.post("/api/sync")
    def sync(payload: SyncRequest, _: dict = Depends(current_session)):
        return service.sync_week(payload.week)

    @app.post("/api/groups/{group_id}/select")
    def select(group_id: str, payload: SelectRequest, _: dict = Depends(current_session)):
        return service.select_group(group_id, payload.publish_date)

    @app.post("/api/groups/{group_id}/unselect")
    def unselect(group_id: str, _: dict = Depends(current_session)):
        return service.unselect_group(group_id)

    @app.patch("/api/groups/{group_id}")
    def patch_group(group_id: str, payload: GroupPatch, _: dict = Depends(current_session)):
        return service.update_group(group_id, title=payload.title, quote=payload.quote)

    @app.put("/api/groups/{group_id}/order")
    def reorder(group_id: str, payload: ReorderRequest, _: dict = Depends(current_session)):
        return service.reorder_group(group_id, payload.photo_ids)

    @app.get("/api/notes")
    def notes(_: dict = Depends(current_session)):
        return {"notes": service.list_notes()}

    @app.post("/api/notes")
    def create_note(payload: NoteCreateRequest, _: dict = Depends(current_session)):
        return service.create_note(
            group_ids=payload.group_ids,
            title=payload.title,
            publish_date=payload.publish_date,
            day=payload.day,
            dynamic_topics=payload.dynamic_topics,
        )

    @app.get("/api/notes/{note_id}")
    def note(note_id: str, _: dict = Depends(current_session)):
        return service.note_detail(note_id)

    @app.patch("/api/notes/{note_id}")
    def patch_note(note_id: str, payload: NotePatch, _: dict = Depends(current_session)):
        return service.update_note(note_id, title=payload.title, content=payload.content, day=payload.day)

    @app.post("/api/notes/{note_id}/topics/refresh")
    def refresh_topics(note_id: str, _: dict = Depends(current_session)):
        return service.refresh_topics(note_id)

    @app.put("/api/notes/{note_id}/topics")
    def save_topics(note_id: str, payload: TopicsRequest, _: dict = Depends(current_session)):
        return service.save_topics(note_id, payload.names)

    @app.post("/api/notes/{note_id}/draft")
    def create_draft(note_id: str, _: dict = Depends(current_session)):
        return service.create_draft(note_id)

    @app.post("/api/notes/{note_id}/open-native")
    def open_native(note_id: str, _: dict = Depends(current_session)):
        return service.open_draft(note_id)

    @app.post("/api/notes/{note_id}/publish")
    def publish(note_id: str, payload: PublishRequest, _: dict = Depends(current_session)):
        return service.publish_note(note_id, confirmed=payload.confirmed)

    @app.post("/api/weeks/{week_id}/close")
    def close_week(week_id: str, _: dict = Depends(current_session)):
        return service.close_week(week_id)

    @app.post("/api/weeks/{week_id}/upload")
    def upload_week(week_id: str, payload: UploadRequest, _: dict = Depends(current_session)):
        return service.queue_upload(week_id, allow_current=payload.allow_current, background=True)

    @app.get("/api/upload-tasks")
    def upload_tasks(week: str | None = None, _: dict = Depends(current_session)):
        return {"tasks": service.upload_tasks(week)}

    @app.post("/api/upload-tasks/{task_id}/retry")
    def retry_task(task_id: str, _: dict = Depends(current_session)):
        return service.retry_upload(task_id, background=True)

    @app.get("/api/settings")
    def settings(_: dict = Depends(current_session)):
        return service.settings_view()

    @app.put("/api/settings")
    def update_settings(payload: dict[str, Any], _: dict = Depends(current_session)):
        return service.update_settings(payload)

    @app.post("/api/settings/password")
    def change_password(payload: PasswordRequest, request: Request):
        try:
            token, expires = auth.change_password(
                payload.current,
                payload.new,
                user_agent=request.headers.get("user-agent", ""),
            )
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        response = JSONResponse({"ok": True})
        _set_cookie(response, request, token, expires)
        service.db.audit("editor", "change_password", "", {})
        return response

    @app.get("/api/settings/autostart")
    def autostart(_: dict = Depends(current_session)):
        return {"items": service.autostart_checklist()}

    @app.get("/api/accounts")
    def accounts(_: dict = Depends(current_session)):
        return {"accounts": service.accounts()}

    @app.post("/api/accounts/import")
    def accounts_import(_: dict = Depends(current_session)):
        return service.import_accounts()

    @app.get("/api/audit")
    def audit(_: dict = Depends(current_session)):
        return {"items": service.db.recent_audit(30)}

    @app.get("/api/health/detail")
    def health_detail(_: dict = Depends(current_session)):
        detail = service.health()
        adapter_error = getattr(service, "adapter_error", "")
        if adapter_error:
            detail["adapter_error"] = adapter_error
        return detail

    @app.get("/media/photo/{photo_id}")
    def photo(photo_id: str, _: dict = Depends(current_session)):
        item = service.repo.get_photo(photo_id)
        if not item or not item.path.is_file():
            raise HTTPException(status_code=404, detail="图片不存在")
        resolved = item.path.resolve()
        allowed = [config.archive_parent.resolve(), config.staging_root.resolve()]
        if not any(_is_relative_to(resolved, root) for root in allowed):
            raise HTTPException(status_code=403, detail="图片路径不在允许目录内")
        return FileResponse(
            resolved,
            headers={"Cache-Control": "private, max-age=3600"},
        )

    if WEBUI_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEBUI_DIR, html=True), name="webui")
    else:
        @app.get("/")
        def missing_ui():
            return {"message": "Web 界面文件缺失，请检查 niguang365/webui"}

    return app


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="逆光365 Web 工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--config", default=None)
    parser.add_argument("--init-auth", action="store_true")
    parser.add_argument("--no-scheduler", action="store_true")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    config.scheduler_enabled = not args.no_scheduler
    service = Service(config) if args.init_auth else None
    if args.init_auth:
        auth = AuthManager(AUTH_PATH, service.db)  # type: ignore[union-attr]
        if auth.initial_password:
            print("已生成初始共享密码（仅显示这一次）：" + auth.initial_password)
        else:
            print("Web 认证配置已存在：" + str(AUTH_PATH))
        return 0

    import uvicorn

    app = create_app(config)
    auth: AuthManager = app.state.auth
    if auth.initial_password:
        print("已生成初始共享密码（仅显示这一次）：" + auth.initial_password)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
