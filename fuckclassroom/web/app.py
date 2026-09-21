from __future__ import annotations

import logging
from collections.abc import Iterable
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..auth.cas import LoginVerificationError
from ..auth.credentials import CredentialStoreError
from ..core.config import PROJECT_ROOT, AppConfig
from ..core.plugins import PluginRuntime
from ..core.tasks import cancel_registered_task
from ..plugins import build_plugin_registry
from ..plugins.classroom.routes import register_routes as register_classroom_routes
from ..plugins.course_selection.routes import register_routes as register_selection_routes


logger = logging.getLogger(__name__)


def _unified_credential_path(config: AppConfig):
    target = config.data_dir / "account" / "credentials.json"
    legacy = config.data_dir / "webvpn" / "credentials.json"
    if target.exists() or not legacy.exists():
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(target)
        return target
    except OSError as exc:
        logger.warning("统一登录凭据迁移失败，暂时沿用旧文件：%s", exc)
        return legacy


def create_app(
    config: AppConfig | None = None,
    enabled_plugins: Iterable[str] | None = None,
) -> FastAPI:
    app_config = config or AppConfig()
    plugin_registry = build_plugin_registry(enabled_plugins)
    plugin_runtime = PluginRuntime(plugin_registry, app_config, logger)
    plugin_runtime.initialize_services()
    services = plugin_runtime.services

    templates = Jinja2Templates(
        directory=plugin_registry.template_directories(PROJECT_ROOT / "templates")
    )

    settings_store = services.get("settings_store")
    cache_store = services.get("cache_store")
    task_manager = services.get("task_manager")
    credential_store = services.get("credential_store")
    verification_broker = services.get("verification_broker")

    classroom_client = services.maybe("classroom_client")
    semester_sync = services.maybe("semester_sync")
    auth_service = services.maybe("auth_service")
    ketangpai_client = services.maybe("ketangpai_client")
    extraction_service = services.maybe("extraction_service")
    download_library = services.maybe("download_library")
    ketangpai_sms_sessions = services.maybe("ketangpai_sms_sessions", {})
    ketangpai_sms_lock = services.maybe("ketangpai_sms_lock")

    hy2_proxy = services.maybe("hy2_proxy")
    course_selection = services.maybe("course_selection")
    auto_selection = services.maybe("auto_selection")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await plugin_runtime.start()
        try:
            yield
        finally:
            await plugin_runtime.stop()

    app = FastAPI(title="Fuck Classroom", lifespan=lifespan)
    app.state.classroom_client = classroom_client
    app.state.ketangpai_client = ketangpai_client
    app.state.get_classroom_session_status = (
        auth_service.get_session_status
        if auth_service is not None
        else lambda: {"is_logged_in": False, "saved_at": ""}
    )
    app.state.get_selection_session_status = (
        course_selection.get_session_status
        if course_selection is not None
        else lambda: {"is_saved": False, "is_valid": False, "checked_at": ""}
    )
    app.state.semester_sync = semester_sync
    app.state.plugin_registry = plugin_registry
    app.state.plugin_runtime = plugin_runtime
    app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")
    for mount in plugin_registry.static_mounts:
        app.mount(mount.path, StaticFiles(directory=str(mount.directory)), name=mount.name)

    core_router = APIRouter()
    classroom_router = APIRouter()
    selection_router = APIRouter()

    def task_started_response(request: Request, task, result_url: str | None = None) -> Response:
        if "application/json" in request.headers.get("accept", ""):
            payload = {
                "task_id": task.id,
                "status_url": f"/api/tasks/{task.id}",
            }
            if result_url:
                payload["result_url"] = result_url
            return JSONResponse(payload)
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    if plugin_registry.is_enabled("classroom"):
        register_classroom_routes(
            classroom_router,
            config=app_config,
            templates=templates,
            classroom_client=classroom_client,
            semester_sync=semester_sync,
            auth_service=auth_service,
            ketangpai_client=ketangpai_client,
            extraction_service=extraction_service,
            download_library=download_library,
            task_manager=task_manager,
            ketangpai_sms_sessions=ketangpai_sms_sessions,
            ketangpai_sms_lock=ketangpai_sms_lock,
            task_started_response=task_started_response,
        )

    if plugin_registry.is_enabled("course_selection"):
        register_selection_routes(
            selection_router,
            templates=templates,
            course_selection=course_selection,
            auto_selection=auto_selection,
            settings_store=settings_store,
            task_manager=task_manager,
            hy2_proxy=hy2_proxy,
            task_started_response=task_started_response,
        )

    @core_router.get("/")
    def index() -> RedirectResponse:
        if plugin_registry.is_enabled("classroom"):
            return RedirectResponse("/classroom", status_code=307)
        if plugin_registry.is_enabled("course_selection"):
            return RedirectResponse("/selection/info", status_code=307)
        return RedirectResponse("/settings", status_code=307)

    @core_router.get("/api/auth/challenge")
    def current_auth_challenge() -> JSONResponse:
        return JSONResponse({"challenge": verification_broker.current()})

    @core_router.post("/api/auth/challenge/{challenge_id}/send")
    def send_auth_code(challenge_id: str) -> JSONResponse:
        try:
            verification_broker.request_send(challenge_id)
        except LoginVerificationError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return JSONResponse({"ok": True})

    @core_router.post("/api/auth/challenge/{challenge_id}/verify")
    async def verify_auth_code(challenge_id: str, request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            verification_broker.request_verify(challenge_id, str(payload.get("code") or ""))
        except (ValueError, AttributeError, LoginVerificationError) as exc:
            return JSONResponse({"detail": str(exc) or "验证码请求无效"}, status_code=409)
        return JSONResponse({"ok": True})

    @core_router.post("/api/auth/challenge/{challenge_id}/cancel")
    def cancel_auth_code(challenge_id: str) -> JSONResponse:
        try:
            verification_broker.request_cancel(challenge_id)
        except LoginVerificationError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return JSONResponse({"ok": True})
    @core_router.get("/settings", response_class=HTMLResponse)
    def settings(request: Request) -> HTMLResponse:
        error = None
        terms = []
        if semester_sync is not None:
            try:
                snapshot = semester_sync.sync()
                terms = list(snapshot.terms)
                if snapshot.error:
                    error = f"学期同步失败，已使用缓存：{snapshot.error}"
            except Exception as exc:  # noqa: BLE001 - optional plugin failure is rendered in UI.
                error = str(exc)
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "session": auth_service.get_session_status(),
                "settings": settings_store.load().to_public_dict(),
                "terms": terms,
                "saved": request.query_params.get("saved") == "1",
                "hy2_built": request.query_params.get("hy2_built") == "1",
                "cache_cleared": request.query_params.get("cache_cleared") == "1",
                "cache_error": request.query_params.get("cache_error"),
                "credential_error": request.query_params.get("credential_error"),
                "credentials_cleared": request.query_params.get("credentials_cleared") == "1",
                "login_state_cleared": request.query_params.get("login_state_cleared") == "1",
                "login_state_error": request.query_params.get("login_state_error"),
                "selection_credentials": credential_store.public_status(),
                "cache_info": cache_store.info(),
                "hy2_status": (
                    hy2_proxy.get_status().to_dict()
                    if hy2_proxy is not None
                    else {"message": "选课插件未启用"}
                ),
                "error": error,
            },
        )

    @core_router.post("/settings")
    async def save_settings(request: Request) -> RedirectResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        form = {key: values[-1] if values else "" for key, values in parsed.items()}
        for field in plugin_registry.active_checkbox_fields:
            form.setdefault(field, "")
        updated = settings_store.update_from_form(form)
        try:
            credential_store.update_from_form(
                form.get("selection_username", ""), form.get("selection_password", "")
            )
        except CredentialStoreError as exc:
            return RedirectResponse(f"/settings?credential_error={quote(str(exc))}", status_code=303)
        if course_selection is not None:
            course_selection.invalidate_access_mode()
        if auto_selection is not None:
            auto_selection.configure_intervals(
                retry_seconds=updated.auto_selection_retry_seconds,
                session_retry_seconds=updated.auto_selection_session_retry_seconds,
                capacity_retry_seconds=updated.auto_selection_capacity_retry_seconds,
            )
        if hy2_proxy is not None and not updated.hy2_enabled:
            hy2_proxy.stop()
        return RedirectResponse("/settings?saved=1", status_code=303)

    @core_router.post("/settings/selection-credentials/clear")
    def clear_selection_credentials() -> RedirectResponse:
        try:
            credential_store.clear()
        except CredentialStoreError as exc:
            return RedirectResponse(f"/settings?credential_error={quote(str(exc))}", status_code=303)
        return RedirectResponse("/settings?credentials_cleared=1", status_code=303)

    @core_router.post("/settings/login-state/clear")
    def clear_login_state() -> RedirectResponse:
        errors: list[str] = []
        clears = []
        if auth_service is not None:
            clears.append(auth_service.clear_session)
        if course_selection is not None:
            clears.append(course_selection.clear_session)
        for clear in clears:
            try:
                clear()
            except OSError as exc:
                errors.append(str(exc))
        if errors:
            return RedirectResponse(
                f"/settings?login_state_error={quote('; '.join(errors))}",
                status_code=303,
            )
        return RedirectResponse("/settings?login_state_cleared=1", status_code=303)

    @core_router.post("/settings/cache/clear")
    def clear_cache() -> RedirectResponse:
        try:
            cache_store.clear()
        except OSError as exc:
            return RedirectResponse(f"/settings?cache_error={quote(str(exc))}", status_code=303)
        return RedirectResponse("/settings?cache_cleared=1", status_code=303)

    @core_router.get("/accounts", response_class=HTMLResponse)
    def accounts(request: Request) -> HTMLResponse:
        classroom_enabled = plugin_registry.is_enabled("classroom")
        selection_enabled = plugin_registry.is_enabled("course_selection")
        sms_session_id = request.query_params.get("ketangpai_sms_session", "")
        sms_session = None
        if classroom_enabled and ketangpai_sms_lock is not None:
            with ketangpai_sms_lock:
                now = time.time()
                for key in list(ketangpai_sms_sessions):
                    if now - float(ketangpai_sms_sessions[key].get("created_at") or 0) > 600:
                        ketangpai_sms_sessions.pop(key, None)
                sms_session = ketangpai_sms_sessions.get(sms_session_id)

        classroom_session = (
            auth_service.get_session_status()
            if auth_service is not None
            else {
                "is_logged_in": False,
                "message": "课程录播插件未启用",
                "saved_at": "",
                "cookie_count": 0,
                "final_url": "",
            }
        )
        classroom_login_run = (
            auth_service.get_login_run_status()
            if auth_service is not None
            else {"message": "插件未启用"}
        )
        selection_session = (
            course_selection.get_session_status()
            if course_selection is not None
            else {
                "is_saved": False,
                "is_valid": False,
                "message": "本科选课插件未启用",
                "saved_at": "",
                "checked_at": "",
                "final_url": "",
            }
        )
        selection_access = (
            course_selection.get_access_info()
            if course_selection is not None
            else {"label": "未启用"}
        )
        selection_credentials = (
            course_selection.get_credential_status()
            if course_selection is not None
            else credential_store.public_status()
        )
        ketangpai_session = (
            ketangpai_client.get_session_status()
            if ketangpai_client is not None
            else {
                "is_valid": False,
                "is_saved": False,
                "message": "课程录播插件未启用",
                "account": "",
                "login_method": "",
            }
        )
        return templates.TemplateResponse(
            request,
            "accounts.html",
            {
                "classroom_enabled": classroom_enabled,
                "selection_enabled": selection_enabled,
                "classroom_session": classroom_session,
                "classroom_login_run": classroom_login_run,
                "selection_session": selection_session,
                "selection_access": selection_access,
                "credentials": selection_credentials,
                "select_error": request.query_params.get("select_error"),
                "ketangpai_session": ketangpai_session,
                "ketangpai_notice": request.query_params.get("ketangpai_notice"),
                "ketangpai_error": request.query_params.get("ketangpai_error"),
                "ketangpai_sms_sent": request.query_params.get("ketangpai_sms_sent") == "1",
                "ketangpai_sms": (
                    {"session_id": sms_session_id, **sms_session}
                    if sms_session is not None
                    else None
                ),
            },
        )

    @core_router.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_status(request: Request, task_id: str) -> HTMLResponse:
        task = task_manager.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        selection_task = any(
            marker in task.title
            for marker in ("选课", "WebVPN", "本科教务", "Hy2", "缓存课程", "退选", "课程信息")
        )
        return templates.TemplateResponse(
            request,
            "task_status.html",
            {
                "task": task,
                "active_system": "selection" if selection_task else "classroom",
                "session": (
                    course_selection.get_session_status()
                    if selection_task and course_selection is not None
                    else (
                        auth_service.get_session_status()
                        if auth_service is not None
                        else {}
                    )
                ),
                "auto_open_outputs": app_config.auto_open_outputs_after_extract and task.title == "提取文字",
            },
        )

    @core_router.get("/tasks", response_class=HTMLResponse)
    def tasks_overview(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "tasks.html",
            {
                "session": (
                    auth_service.get_session_status()
                    if auth_service is not None
                    else {}
                ),
                "tasks": task_manager.list_recent(),
            },
        )

    @core_router.get("/api/tasks/{task_id}")
    def api_task_status(task_id: str) -> dict[str, object]:
        task = task_manager.as_dict(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task

    @core_router.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str) -> dict[str, object]:
        try:
            return cancel_registered_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    @core_router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    plugin_runtime.attach_router("core_ui", core_router)
    plugin_runtime.attach_router("classroom", classroom_router)
    plugin_runtime.attach_router("course_selection", selection_router)
    plugin_runtime.include_routes(app)
    return app
