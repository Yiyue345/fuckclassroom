from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import parse_qs
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..auth import LoginSessionService
from ..auth.cas import LoginVerificationBroker, LoginVerificationError
from ..auth.credentials import CredentialStoreError, SelectionCredentialStore
from ..classroom import (
    ClassroomClient,
    ClassroomClientError,
    CourseDetail,
    SemesterSyncService,
    rewrite_live_playlist,
)
from ..classroom.downloads import DownloadLibrary
from ..classroom.extraction import ExtractionError, LessonExtractionService
from ..core.cache import JsonCache
from ..core.config import PROJECT_ROOT, AppConfig
from ..core.settings import SettingsStore
from ..core.tasks import TaskManager
from ..course_selection import (
    CourseSelectionApiError,
    CourseSelectionAssistant,
    SELECTION_INFO_VIEWS,
    SelectionTarget,
)
from ..course_selection.automation import AutoSelectionService
from ..course_selection.helpers import _group_plan_courses
from ..course_selection.proxy import Hy2ProxyManager


templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))
logger = logging.getLogger(__name__)


def _resolve_catalog_queue_batch(
    batches: list[dict[str, object]],
    batch_key: str,
) -> dict[str, object] | None:
    if batch_key == "pending":
        return None
    try:
        student_id_text, turn_id_text = batch_key.split(":", 1)
        student_id = int(student_id_text)
        turn_id = int(turn_id_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("请选择有效的定时选课批次") from exc

    batch = next(
        (
            item
            for item in batches
            if str(item.get("student_id") or "") == str(student_id)
            and str(item.get("turn_id") or "") == str(turn_id)
        ),
        None,
    )
    if batch is None:
        raise ValueError("所选定时选课批次已不可用，请刷新后重试")
    return dict(batch)


def _login_and_refresh_selection_info(
    course_selection: CourseSelectionAssistant,
    progress: Callable[[int, str], None],
) -> str:
    def login_progress(value: int, message: str) -> None:
        progress(round(value * 0.7), message)

    def refresh_progress(value: int, message: str) -> None:
        progress(70 + round(value * 0.3), message)

    result_url = course_selection.login_for_web(
        progress=login_progress,
        force_interactive=True,
    )
    return course_selection.refresh_selection_info_for_web(
        progress=refresh_progress,
        result_url=result_url,
    )


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


def _run_for_result(operation: Callable[[], object], result_url: str) -> str:
    operation()
    return result_url


def create_app(config: AppConfig | None = None) -> FastAPI:
    app_config = config or AppConfig()
    classroom_client = ClassroomClient(app_config)
    semester_sync = SemesterSyncService(
        classroom_client,
        app_config.cache_dir / "classroom" / "semesters.json",
    )
    hy2_proxy = Hy2ProxyManager(app_config)
    credential_store = SelectionCredentialStore(_unified_credential_path(app_config))
    verification_broker = LoginVerificationBroker()
    login_lock = threading.Lock()
    auth_service = LoginSessionService(
        app_config,
        credential_store=credential_store,
        verification_broker=verification_broker,
        login_lock=login_lock,
    )
    course_selection = CourseSelectionAssistant(
        app_config,
        hy2_proxy=hy2_proxy,
        credential_store=credential_store,
        verification_broker=verification_broker,
        login_lock=login_lock,
    )
    extraction_service = LessonExtractionService(app_config)
    settings_store = SettingsStore(app_config.data_dir / "settings.json")
    cache_store = JsonCache(app_config.cache_dir)
    download_library = DownloadLibrary(app_config.downloads_dir)
    task_manager = TaskManager()
    runtime_settings = settings_store.load()
    auto_selection = AutoSelectionService(
        course_selection,
        app_config.data_dir / "webvpn" / "auto_selection_jobs.json",
        retry_seconds=runtime_settings.auto_selection_retry_seconds,
        session_retry_seconds=runtime_settings.auto_selection_session_retry_seconds,
        capacity_retry_seconds=runtime_settings.auto_selection_capacity_retry_seconds,
    )

    def verify_classroom_session():
        return auth_service.verify_saved_session(auto_relogin=True)

    def enrich_live_status(detail: CourseDetail) -> CourseDetail:
        """Overlay the uncached live-list state onto cached course-detail rows."""
        try:
            live_ids = {
                item.lesson_id
                for item in classroom_client.list_current_live_lessons([detail.course])
            }
        except ClassroomClientError:
            return detail
        if not live_ids:
            return detail
        return CourseDetail(
            course=detail.course,
            lessons=[
                replace(lesson, is_live=True, status_label="直播中")
                if lesson.id in live_ids
                else lesson
                for lesson in detail.lessons
            ],
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def sync_semesters() -> None:
            try:
                snapshot = await asyncio.to_thread(semester_sync.sync, force=True)
            except Exception as exc:  # noqa: BLE001 - semester sync must not stop startup.
                logger.warning("启动时学期同步失败：%s", exc)
                return
            if snapshot.error:
                logger.warning("启动时学期同步失败，已使用缓存：%s", snapshot.error)
            else:
                logger.info("启动时已同步 %s 个学期", len(snapshot.terms))

        async def verify_saved_sessions() -> None:
            results = await asyncio.gather(
                asyncio.to_thread(verify_classroom_session),
                asyncio.to_thread(
                    course_selection.verify_saved_session,
                    timeout_ms=60_000,
                    auto_relogin=True,
                ),
                return_exceptions=True,
            )
            labels = ("课程录播", "WebVPN/教务")
            for label, result in zip(labels, results):
                if isinstance(result, Exception):
                    logger.warning("启动时%s会话检测失败：%s", label, result)
                else:
                    logger.info("启动时%s会话状态：%s", label, result.message)
            selection_result = results[1]
            if not isinstance(selection_result, Exception) and selection_result.is_valid is True:
                try:
                    await asyncio.to_thread(
                        course_selection.refresh_selection_info_for_web,
                        timeout_ms=60_000,
                    )
                except Exception as exc:  # noqa: BLE001 - startup refresh must not stop the app.
                    logger.warning("启动时选课信息刷新失败：%s", exc)

        startup_check = asyncio.create_task(verify_saved_sessions())
        semester_check = asyncio.create_task(sync_semesters())
        auto_selection.start()
        try:
            yield
        finally:
            for task in (startup_check, semester_check):
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            auto_selection.stop()
            hy2_proxy.stop()

    app = FastAPI(title="Fuck Classroom", lifespan=lifespan)
    app.state.get_classroom_session_status = auth_service.get_session_status
    app.state.get_selection_session_status = course_selection.get_session_status
    app.state.semester_sync = semester_sync
    app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")
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

    @app.get("/")
    def index() -> RedirectResponse:
        return RedirectResponse("/classroom", status_code=307)

    @app.get("/classroom", response_class=HTMLResponse)
    def classroom_home(request: Request) -> HTMLResponse:
        session = auth_service.get_session_status()
        courses = []
        course_error = None
        live_lessons = []
        live_error = None
        if session.is_logged_in:
            try:
                courses = classroom_client.list_courses(
                    page_size=100,
                    term_id=app_config.default_term_id or None,
                )
            except ClassroomClientError as exc:
                course_error = str(exc)
            if courses:
                try:
                    live_lessons = classroom_client.list_current_live_lessons(courses)
                except ClassroomClientError as exc:
                    live_error = str(exc)
        files = download_library.list_files()
        recent_tasks = task_manager.list_recent(4)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "session": session,
                "courses": courses,
                "course_error": course_error,
                "live_lessons": live_lessons,
                "live_error": live_error,
                "files": files,
                "recent_tasks": recent_tasks,
                "running_task_count": sum(task.status == "running" for task in recent_tasks),
                "completed_task_count": sum(task.status == "succeeded" for task in recent_tasks),
            },
        )

    @app.post("/auth/start")
    def start_login(request: Request) -> Response:
        task = task_manager.start(
            "课程录播登录",
            lambda progress: _run_for_result(
                lambda: auth_service.login_for_web(progress),
                "/accounts#classroom-session",
            ),
        )
        return task_started_response(request, task, "/accounts#classroom-session")

    @app.post("/auth/verify")
    def verify_login(request: Request) -> Response:
        task = task_manager.start(
            "验证课程录播会话",
            lambda progress: _run_for_result(
                lambda: auth_service.verify_for_web(progress),
                "/accounts#classroom-session",
            ),
        )
        return task_started_response(request, task, "/accounts#classroom-session")

    @app.post("/auth/logout")
    def logout() -> RedirectResponse:
        auth_service.clear_session()
        return RedirectResponse("/accounts#classroom-session", status_code=303)

    @app.get("/api/auth/challenge")
    def current_auth_challenge() -> JSONResponse:
        return JSONResponse({"challenge": verification_broker.current()})

    @app.post("/api/auth/challenge/{challenge_id}/send")
    def send_auth_code(challenge_id: str) -> JSONResponse:
        try:
            verification_broker.request_send(challenge_id)
        except LoginVerificationError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return JSONResponse({"ok": True})

    @app.post("/api/auth/challenge/{challenge_id}/verify")
    async def verify_auth_code(challenge_id: str, request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            verification_broker.request_verify(challenge_id, str(payload.get("code") or ""))
        except (ValueError, AttributeError, LoginVerificationError) as exc:
            return JSONResponse({"detail": str(exc) or "验证码请求无效"}, status_code=409)
        return JSONResponse({"ok": True})

    @app.post("/api/auth/challenge/{challenge_id}/cancel")
    def cancel_auth_code(challenge_id: str) -> JSONResponse:
        try:
            verification_broker.request_cancel(challenge_id)
        except LoginVerificationError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return JSONResponse({"ok": True})
    @app.get("/courses", response_class=HTMLResponse)
    def courses(request: Request, term_id: str | None = None) -> HTMLResponse:
        error = None
        term_error = None
        courses = []
        terms = []
        selected_term_id = term_id if term_id is not None else ""
        selected_term = None
        try:
            snapshot = semester_sync.sync(force=True)
            terms = list(snapshot.terms)
            if term_id is None:
                selected_term_id = snapshot.current_term_id or app_config.default_term_id
            selected_term = next((item for item in terms if item.id == selected_term_id), None)
            if snapshot.error:
                term_error = f"学期同步失败，已使用缓存：{snapshot.error}"
        except ClassroomClientError as exc:
            term_error = f"学期列表加载失败：{exc}"

        try:
            courses = classroom_client.list_courses(term=selected_term, force_refresh=True)
        except ClassroomClientError as exc:
            error = str(exc)

        return templates.TemplateResponse(
            request,
            "courses.html",
            {
                "session": auth_service.get_session_status(),
                "courses": courses,
                "terms": terms,
                "selected_term_id": selected_term_id,
                "term_error": term_error,
                "error": error,
            },
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request) -> HTMLResponse:
        error = None
        terms = []
        try:
            snapshot = semester_sync.sync()
            terms = list(snapshot.terms)
            if snapshot.error:
                error = f"学期同步失败，已使用缓存：{snapshot.error}"
        except ClassroomClientError as exc:
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
                "hy2_status": hy2_proxy.get_status().to_dict(),
                "error": error,
            },
        )

    @app.post("/settings")
    async def save_settings(request: Request) -> RedirectResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        form = {key: values[-1] if values else "" for key, values in parsed.items()}
        updated = settings_store.update_from_form(form)
        try:
            credential_store.update_from_form(
                form.get("selection_username", ""), form.get("selection_password", "")
            )
        except CredentialStoreError as exc:
            return RedirectResponse(f"/settings?credential_error={quote(str(exc))}", status_code=303)
        course_selection.invalidate_access_mode()
        auto_selection.configure_intervals(
            retry_seconds=updated.auto_selection_retry_seconds,
            session_retry_seconds=updated.auto_selection_session_retry_seconds,
            capacity_retry_seconds=updated.auto_selection_capacity_retry_seconds,
        )
        if not updated.hy2_enabled:
            hy2_proxy.stop()
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/hy2/build")
    async def build_hy2_proxy(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        form = {key: values[-1] if values else "" for key, values in parsed.items()}
        updated = settings_store.update_from_form(form)
        auto_selection.configure_intervals(
            retry_seconds=updated.auto_selection_retry_seconds,
            session_retry_seconds=updated.auto_selection_session_retry_seconds,
            capacity_retry_seconds=updated.auto_selection_capacity_retry_seconds,
        )
        course_selection.invalidate_access_mode()
        hy2_proxy.stop()
        task = task_manager.start("构建 Hy2 代理", hy2_proxy.build_runner)
        return task_started_response(request, task, "/settings?hy2_built=1")

    @app.post("/settings/selection-credentials/clear")
    def clear_selection_credentials() -> RedirectResponse:
        try:
            credential_store.clear()
        except CredentialStoreError as exc:
            return RedirectResponse(f"/settings?credential_error={quote(str(exc))}", status_code=303)
        return RedirectResponse("/settings?credentials_cleared=1", status_code=303)

    @app.post("/settings/login-state/clear")
    def clear_login_state() -> RedirectResponse:
        errors: list[str] = []
        for clear in (auth_service.clear_session, course_selection.clear_session):
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

    @app.post("/settings/cache/clear")
    def clear_cache() -> RedirectResponse:
        try:
            cache_store.clear()
        except OSError as exc:
            return RedirectResponse(f"/settings?cache_error={quote(str(exc))}", status_code=303)
        return RedirectResponse("/settings?cache_cleared=1", status_code=303)

    @app.get("/courses/{course_id}", response_class=HTMLResponse)
    def course_detail(request: Request, course_id: str) -> HTMLResponse:
        error = None
        detail = None
        try:
            detail = enrich_live_status(classroom_client.refresh_course_detail(course_id))
        except ClassroomClientError as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request,
            "course_detail.html",
            {
                "session": auth_service.get_session_status(),
                "detail": detail,
                "course_id": course_id,
                "refreshed": request.query_params.get("refreshed") == "1",
                "refresh_error": request.query_params.get("refresh_error"),
                "error": error,
            },
        )

    @app.post("/courses/{course_id}/refresh")
    def refresh_course_detail(course_id: str) -> RedirectResponse:
        return RedirectResponse(f"/courses/{course_id}?refreshed=1", status_code=303)

    @app.get("/courses/{course_id}/lessons/{lesson_id}/download/{export_kind}")
    def download_lesson_export(course_id: str, lesson_id: str, export_kind: str) -> Response:
        try:
            exported = classroom_client.download_lesson_export(course_id, lesson_id, export_kind)
        except ClassroomClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        quoted_name = quote(exported.filename)
        return Response(
            content=exported.content,
            media_type=exported.content_type,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted_name}"},
        )

    @app.get("/downloads", response_class=HTMLResponse)
    def downloads(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "downloads.html",
            {
                "session": auth_service.get_session_status(),
                "files": download_library.list_files(),
                "deleted": request.query_params.get("deleted") == "1",
                "delete_error": request.query_params.get("delete_error"),
            },
        )

    @app.get("/downloads/file/{relative_path:path}")
    def download_local_file(relative_path: str) -> FileResponse:
        try:
            path = download_library.resolve(relative_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="文件不存在") from exc
        return FileResponse(path, filename=path.name)

    @app.post("/downloads/local/{action}")
    async def local_download_action(action: str, request: Request) -> JSONResponse:
        if action not in {"open", "reveal"}:
            return JSONResponse({"detail": "不支持的文件操作"}, status_code=400)
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        relative_path = (parsed.get("relative_path") or [""])[-1]
        action_label = "打开文件" if action == "open" else "在文件夹中显示"
        try:
            path = (
                download_library.open_file(relative_path)
                if action == "open"
                else download_library.reveal_file(relative_path)
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except FileNotFoundError:
            return JSONResponse({"detail": "文件不存在"}, status_code=404)
        except OSError as exc:
            return JSONResponse({"detail": f"{action_label}失败：{exc}"}, status_code=409)
        message = (
            f"已使用系统默认应用打开 {path.name}"
            if action == "open"
            else f"已在文件夹中定位 {path.name}"
        )
        return JSONResponse({"action": action, "message": message})

    @app.post("/downloads/delete")
    async def delete_download(request: Request) -> RedirectResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        relative_path = (parsed.get("relative_path") or [""])[-1]
        try:
            download_library.delete(relative_path)
        except (FileNotFoundError, ValueError, OSError) as exc:
            return RedirectResponse(f"/downloads?delete_error={quote(str(exc))}", status_code=303)
        return RedirectResponse("/downloads?deleted=1", status_code=303)

    @app.post("/downloads/delete-many")
    async def delete_downloads(request: Request) -> JSONResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        relative_paths = [value for value in parsed.get("relative_path", []) if value]
        if not relative_paths:
            return JSONResponse({"detail": "请先选择要删除的文件"}, status_code=400)
        if len(relative_paths) > 500:
            return JSONResponse({"detail": "单次最多删除 500 个文件"}, status_code=400)
        try:
            deleted_count = download_library.delete_many(relative_paths)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except FileNotFoundError:
            return JSONResponse({"detail": "部分文件已不存在，请刷新后重试"}, status_code=404)
        except OSError as exc:
            return JSONResponse({"detail": f"批量删除失败：{exc}"}, status_code=409)
        return JSONResponse(
            {
                "deleted": deleted_count,
                "message": f"已删除 {deleted_count} 个文件",
            }
        )

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "accounts.html",
            {
                "classroom_session": auth_service.get_session_status(),
                "classroom_login_run": auth_service.get_login_run_status(),
                "selection_session": course_selection.get_session_status(),
                "selection_access": course_selection.get_access_info(),
                "credentials": course_selection.get_credential_status(),
                "select_error": request.query_params.get("select_error"),
            },
        )

    @app.get("/selection")
    def selection() -> RedirectResponse:
        return RedirectResponse("/accounts#selection-session", status_code=307)

    @app.get("/selection/catalog", response_class=HTMLResponse)
    def selection_catalog(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "selection_catalog.html",
            {
                "session": course_selection.get_session_status(),
                "catalog_targets": settings_store.load().catalog_lesson_name_targets,
            },
        )

    @app.get("/api/selection/catalog")
    def selection_catalog_api(request: Request) -> JSONResponse:
        allowed = {
            "semester_id",
            "course_code",
            "course_name",
            "lesson_code",
            "lesson_name",
            "room",
            "teacher_id",
            "department_id",
            "campus_id",
            "course_type_id",
            "exam_mode_id",
            "grade",
            "week_start",
            "week_end",
            "weekday",
            "max_count_min",
            "max_count_max",
            "selected_count_min",
            "selected_count_max",
            "credits_min",
            "credits_max",
            "compulsory",
        }
        filters = {
            key: request.query_params.get(key, "")
            for key in allowed
            if request.query_params.get(key, "") != ""
        }
        filters["periods"] = request.query_params.getlist("periods")
        try:
            page = int(request.query_params.get("page", "1"))
            page_size = int(request.query_params.get("page_size", "50"))
            targets = settings_store.load().catalog_lesson_name_targets
            targets_only = request.query_params.get("targets_only") == "1" and bool(targets)
            if targets_only and targets:
                merged: dict[int, dict[str, object]] = {}
                first_result = None
                for target in targets:
                    target_filters = dict(filters)
                    target_filters["lesson_name"] = target
                    result = course_selection.query_whole_school_courses(
                        target_filters,
                        page=1,
                        page_size=100,
                    )
                    if first_result is None:
                        first_result = result
                    for course in result["courses"]:
                        lesson_id = course.get("lesson_id")
                        if isinstance(lesson_id, int):
                            merged[lesson_id] = course
                result = first_result or course_selection.query_whole_school_courses(
                    filters,
                    page=1,
                    page_size=20,
                )
                result["courses"] = list(merged.values())
                total = len(result["courses"])
                result["pagination"] = {
                    "page": 1,
                    "page_size": total or 1,
                    "total": total,
                    "pages": 1,
                }
            else:
                result = course_selection.query_whole_school_courses(
                    filters,
                    page=page,
                    page_size=page_size,
                )
            queued_ids = auto_selection.active_lesson_ids()
            for course in result["courses"]:
                lesson_name = str(course.get("lesson_name") or "").casefold()
                course["matched_targets"] = [
                    target for target in targets if target.casefold() in lesson_name
                ]
                course["is_queued"] = course.get("lesson_id") in queued_ids
            selection_info = course_selection.get_selection_info("all")
            result["selection_batches"] = selection_info.batches
            result["targets"] = list(targets)
            result["targets_only"] = targets_only
            return JSONResponse(result)
        except (CourseSelectionApiError, RuntimeError, TypeError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/selection/catalog/teachers")
    def selection_catalog_teachers_api(q: str = "") -> JSONResponse:
        try:
            return JSONResponse(
                {"teachers": course_selection.query_course_catalog_teachers(q)}
            )
        except (CourseSelectionApiError, RuntimeError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.post("/selection/catalog/action")
    async def selection_catalog_action(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        action = (parsed.get("action") or [""])[-1]
        try:
            lesson_id = int((parsed.get("lesson_id") or [""])[-1])
            course_selection.get_cached_catalog_course(lesson_id)
            if action not in {"select", "drop"}:
                raise ValueError("课程操作无效")
        except (TypeError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

        label = "退课" if action == "drop" else "选课"
        task = task_manager.start(
            f"全校开课{label}",
            lambda progress: course_selection.run_catalog_lesson_action_for_web(
                lesson_id,
                action,
                progress=progress,
            ),
        )
        return task_started_response(request, task, "/selection/catalog")

    @app.post("/selection/catalog/queue")
    async def selection_catalog_queue(request: Request) -> JSONResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        try:
            lesson_id = int((parsed.get("lesson_id") or [""])[-1])
            course = course_selection.get_cached_catalog_course(lesson_id)
            batch_key = (parsed.get("batch_key") or [""])[-1]
            batches = course_selection.get_selection_info("all").batches
            batch = _resolve_catalog_queue_batch(batches, batch_key)
            job, created = auto_selection.add_job(lesson_id, course, batch)
        except (TypeError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "job": job.to_dict(display=True),
                "message": "已加入定时队列" if created else "这门课已经在定时队列中",
            }
        )

    @app.get("/selection/info", response_class=HTMLResponse)
    def selection_info(request: Request) -> HTMLResponse:
        return _selection_info_response(request, "major-plan")

    @app.get("/selection/info/{view}", response_class=HTMLResponse)
    def selection_info_view(request: Request, view: str) -> HTMLResponse:
        if view not in SELECTION_INFO_VIEWS:
            raise HTTPException(status_code=404, detail="选课信息页面不存在")
        return _selection_info_response(request, view)

    def _selection_info_response(request: Request, view: str) -> HTMLResponse:
        info = course_selection.get_selection_info(view)
        course_sections = (
            _group_plan_courses(info.courses)
            if info.active_view == "major-plan"
            else [{"label": "", "courses": info.courses}]
        )
        return templates.TemplateResponse(
            request,
            "selection_info.html",
            {
                "session": course_selection.get_session_status(),
                "info": info,
                "course_sections": course_sections,
                "views": SELECTION_INFO_VIEWS,
                "last_action": course_selection.pop_selection_action_result(),
                "select_error": request.query_params.get("select_error"),
                "auto_lesson_ids": auto_selection.active_lesson_ids(),
            },
        )

    @app.get("/selection/auto", response_class=HTMLResponse)
    def selection_auto_jobs(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "selection_auto.html",
            {
                "session": course_selection.get_session_status(),
                "jobs": auto_selection.list_jobs(),
                "select_error": request.query_params.get("select_error"),
            },
        )

    @app.get("/api/selection/auto/jobs")
    def selection_auto_jobs_api() -> JSONResponse:
        return JSONResponse({"jobs": auto_selection.list_jobs()})

    @app.post("/selection/auto/add")
    async def add_selection_auto_job(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        raw_lesson_id = (parsed.get("lesson_id") or [""])[-1]
        try:
            lesson_id = int(raw_lesson_id)
            info = course_selection.get_selection_info("all")
            batch = info.current_batch
            course = next(
                (
                    item
                    for courses in info.sections.values()
                    for item in courses
                    if isinstance(item, dict) and item.get("lesson_id") == lesson_id
                ),
                None,
            )
            if batch is None:
                raise ValueError("尚未缓存选课批次，请先刷新课程")
            if course is None:
                raise ValueError("缓存中找不到这门课，请先刷新课程")
            job, created = auto_selection.add_job(lesson_id, course, batch)
        except (TypeError, ValueError) as exc:
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": str(exc)}, status_code=400)
            return RedirectResponse(f"/selection/info?select_error={quote(str(exc))}", status_code=303)

        message = "已加入定时选课" if created else "这门课已经在定时队列中"
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"job": job.to_dict(display=True), "message": message})
        return RedirectResponse("/selection/auto", status_code=303)

    @app.post("/selection/auto/{job_id}/cancel")
    def cancel_selection_auto_job(job_id: str) -> Response:
        try:
            auto_selection.cancel(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="定时选课任务不存在")
        return RedirectResponse("/selection/auto", status_code=303)

    @app.post("/selection/auto/{job_id}/resume")
    def resume_selection_auto_job(job_id: str) -> Response:
        try:
            auto_selection.resume(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="定时选课任务不存在")
        return RedirectResponse("/selection/auto", status_code=303)

    @app.post("/selection/auto/{job_id}/delete")
    def delete_selection_auto_job(job_id: str) -> Response:
        try:
            auto_selection.delete(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="定时选课任务不存在")
        except ValueError as exc:
            return RedirectResponse(f"/selection/auto?select_error={quote(str(exc))}", status_code=303)
        return RedirectResponse("/selection/auto", status_code=303)


    @app.post("/selection/info/refresh")
    async def refresh_selection_info(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        view = (parsed.get("view") or ["major-plan"])[-1]
        result_url = f"/selection/info/{view}" if view in SELECTION_INFO_VIEWS and view != "major-plan" else "/selection/info"
        batch_key = (parsed.get("batch_key") or [""])[-1].strip()
        student_id = None
        turn_id = None
        if batch_key:
            try:
                raw_student_id, raw_turn_id = batch_key.split(":", 1)
                student_id = int(raw_student_id)
                turn_id = int(raw_turn_id)
            except (TypeError, ValueError):
                detail = "选课批次无效，请重新选择"
                if "application/json" in request.headers.get("accept", ""):
                    return JSONResponse({"detail": detail}, status_code=400)
                return RedirectResponse(f"{result_url}?select_error={quote(detail)}", status_code=303)
        task = task_manager.start(
            "切换选课批次" if batch_key else "刷新选课信息",
            lambda progress: course_selection.refresh_selection_info_for_web(
                progress=progress,
                result_url=result_url,
                student_id=student_id,
                turn_id=turn_id,
            ),
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {
                    "task_id": task.id,
                    "status_url": f"/api/tasks/{task.id}",
                    "result_url": result_url,
                }
            )
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @app.get("/api/selection/counts")
    def live_selection_counts() -> JSONResponse:
        try:
            return JSONResponse(course_selection.get_live_selection_counts())
        except CourseSelectionApiError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/selection/action-result")
    def selection_action_result() -> JSONResponse:
        return JSONResponse(course_selection.pop_selection_action_result() or {})

    @app.post("/selection/info/select")
    async def select_cached_lesson(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        view = (parsed.get("view") or ["major-plan"])[-1]
        raw_lesson_id = (parsed.get("lesson_id") or [""])[-1]
        try:
            lesson_id = int(raw_lesson_id)
        except ValueError:
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": "课程编号无效"}, status_code=400)
            return RedirectResponse(f"/selection/info?select_error={quote('课程编号无效')}", status_code=303)
        result_url = f"/selection/info/{view}" if view in SELECTION_INFO_VIEWS and view != "major-plan" else "/selection/info"
        task = task_manager.start(
            "选择缓存课程",
            lambda progress: course_selection.select_cached_lesson_for_web(lesson_id, progress=progress, result_url=result_url),
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({
                "task_id": task.id,
                "status_url": f"/api/tasks/{task.id}",
                "result_url": result_url,
            })
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @app.post("/selection/info/drop")
    async def drop_cached_lesson(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        view = (parsed.get("view") or ["selected"])[-1]
        raw_lesson_id = (parsed.get("lesson_id") or [""])[-1]
        try:
            lesson_id = int(raw_lesson_id)
        except ValueError:
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": "课程编号无效"}, status_code=400)
            return RedirectResponse(f"/selection/info/selected?select_error={quote('课程编号无效')}", status_code=303)
        result_url = f"/selection/info/{view}" if view in SELECTION_INFO_VIEWS and view != "major-plan" else "/selection/info"
        task = task_manager.start(
            "退选缓存课程",
            lambda progress: course_selection.drop_cached_lesson_for_web(lesson_id, progress=progress, result_url=result_url),
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({
                "task_id": task.id,
                "status_url": f"/api/tasks/{task.id}",
                "result_url": result_url,
            })
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @app.post("/selection/login")
    def start_webvpn_login(request: Request) -> Response:
        task = task_manager.start(
            "本科教务登录",
            lambda progress: _run_for_result(
                lambda: _login_and_refresh_selection_info(course_selection, progress),
                "/accounts#selection-session",
            ),
        )
        return task_started_response(request, task, "/accounts#selection-session")

    @app.post("/selection/open")
    def open_selection_home(request: Request) -> Response:
        task = task_manager.start(
            "打开本科教务",
            lambda progress: _run_for_result(
                lambda: course_selection.open_home_for_web(progress=progress),
                "/accounts#selection-session",
            ),
        )
        return task_started_response(request, task, "/accounts#selection-session")

    @app.post("/selection/open-course-select")
    def open_course_select(request: Request) -> Response:
        task = task_manager.start(
            "打开选课入口",
            lambda progress: _run_for_result(
                lambda: course_selection.open_course_select_for_web(progress=progress),
                "/accounts#selection-session",
            ),
        )
        return task_started_response(request, task, "/accounts#selection-session")

    @app.post("/selection/logout")
    def selection_logout() -> RedirectResponse:
        course_selection.clear_session()
        return RedirectResponse("/accounts#selection-session", status_code=303)

    @app.post("/selection/capture")
    async def capture_selection_interfaces(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        raw_duration = (parsed.get("duration_seconds") or ["180"])[-1]
        try:
            duration_seconds = max(30, min(900, int(raw_duration)))
        except ValueError:
            duration_seconds = 180
        task = task_manager.start(
            "抓取选课接口",
            lambda progress: _run_for_result(
                lambda: course_selection.capture_interfaces_for_web(
                    progress=progress,
                    duration_seconds=duration_seconds,
                ),
                "/selection/captures",
            ),
        )
        return task_started_response(request, task, "/selection/captures")

    @app.get("/selection/captures", response_class=HTMLResponse)
    def selection_captures(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "selection_captures.html",
            {
                "session": course_selection.get_session_status(),
                "captures": course_selection.list_captures(),
            },
        )

    @app.get("/selection/captures/{name}", response_class=HTMLResponse)
    def selection_capture_detail(request: Request, name: str) -> HTMLResponse:
        try:
            content = course_selection.read_capture_markdown(name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="抓取记录不存在") from exc
        return templates.TemplateResponse(
            request,
            "selection_capture_detail.html",
            {
                "session": course_selection.get_session_status(),
                "name": name,
                "content": content,
            },
        )

    @app.post("/selection/highlight")
    async def highlight_selection_course(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        target = SelectionTarget(
            course_name=(parsed.get("name") or [""])[-1].strip(),
            course_code=(parsed.get("code") or [""])[-1].strip(),
            teacher=(parsed.get("teacher") or [""])[-1].strip(),
        )
        task = task_manager.start(
            "定位选课课程",
            lambda progress: course_selection.highlight_for_web(target, progress=progress),
        )
        return task_started_response(request, task, "/selection")

    @app.post("/selection/select-once")
    async def select_course_once(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        if (parsed.get("confirm_select") or [""])[-1] != "YES":
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": "需要先确认只点击一次唯一匹配课程"}, status_code=400)
            return RedirectResponse(f"/selection?select_error={quote('需要先确认只点击一次唯一匹配课程')}", status_code=303)
        target = SelectionTarget(
            course_name=(parsed.get("name") or [""])[-1].strip(),
            course_code=(parsed.get("code") or [""])[-1].strip(),
            teacher=(parsed.get("teacher") or [""])[-1].strip(),
        )
        task = task_manager.start(
            "执行一次选课",
            lambda progress: course_selection.select_once_for_web(target, progress=progress),
        )
        return task_started_response(request, task, "/selection")

    @app.post("/courses/{course_id}/lessons/{lesson_id}/extract")
    def extract_lesson(request: Request, course_id: str, lesson_id: str) -> Response:
        result_url = f"/courses/{course_id}/lessons/{lesson_id}/outputs"

        def worker(progress):
            extraction_service.extract_lesson(course_id, lesson_id, run_ocr=True, progress=progress)
            return result_url

        task = task_manager.start("提取文字", worker)
        return task_started_response(request, task, result_url)

    @app.post("/courses/{course_id}/lessons/{lesson_id}/summarize")
    def summarize_lesson(request: Request, course_id: str, lesson_id: str) -> Response:
        result_url = f"/courses/{course_id}/lessons/{lesson_id}/outputs"

        def worker(progress):
            extraction_service.summarize_lesson(course_id, lesson_id, progress=progress)
            return result_url

        task = task_manager.start("生成 AI 总结", worker)
        return task_started_response(request, task, result_url)

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
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
                    if selection_task
                    else auth_service.get_session_status()
                ),
                "auto_open_outputs": app_config.auto_open_outputs_after_extract and task.title == "提取文字",
            },
        )

    @app.get("/tasks", response_class=HTMLResponse)
    def tasks_overview(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "tasks.html",
            {
                "session": auth_service.get_session_status(),
                "tasks": task_manager.list_recent(),
            },
        )

    @app.get("/api/tasks/{task_id}")
    def api_task_status(task_id: str) -> dict[str, object]:
        task = task_manager.as_dict(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task

    @app.get("/courses/{course_id}/lessons/{lesson_id}/outputs", response_class=HTMLResponse)
    def lesson_outputs(request: Request, course_id: str, lesson_id: str) -> HTMLResponse:
        error = None
        outputs = {}
        try:
            outputs = extraction_service.get_existing_outputs(course_id, lesson_id)
        except (ClassroomClientError, ExtractionError) as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request,
            "lesson_outputs.html",
            {
                "session": auth_service.get_session_status(),
                "course_id": course_id,
                "lesson_id": lesson_id,
                "outputs": outputs,
                "error": error,
            },
        )

    @app.get("/api/courses")
    def api_courses(term_id: str | None = None) -> dict[str, object]:
        courses = classroom_client.list_courses(term_id=term_id or None)
        return {"courses": [course.__dict__ for course in courses]}

    @app.get("/api/courses/{course_id}/lessons/{lesson_id}/live")
    def api_live_lesson(course_id: str, lesson_id: str) -> dict[str, object]:
        try:
            info = classroom_client.get_live_lesson_info(course_id, lesson_id)
        except ClassroomClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        qualities = [
            {
                "id": quality,
                "label": label,
                "playlist_url": (
                    f"/api/courses/{quote(course_id, safe='')}/lessons/"
                    f"{quote(lesson_id, safe='')}/live/playlist?quality={quality}"
                ),
            }
            for quality, label in (
                ("original", "原画"),
                ("high", "高清"),
                ("standard", "流畅"),
            )
            if quality in info.playlist_urls
        ]
        return {
            "course_id": info.course_id,
            "lesson_id": info.lesson_id,
            "course_title": info.course_title,
            "lesson_title": info.lesson_title,
            "lecturer": info.lecturer,
            "room_name": info.room_name,
            "start_at": info.start_at,
            "end_at": info.end_at,
            "is_live": info.is_live,
            "ppt_running": info.ppt_running,
            "official_url": info.official_url,
            "qualities": qualities,
            "playlist_url": qualities[0]["playlist_url"] if qualities else "",
        }

    @app.get("/api/courses/{course_id}/lessons/{lesson_id}/live/playlist")
    def api_live_playlist(course_id: str, lesson_id: str, quality: str = "original") -> Response:
        try:
            source_url = classroom_client.get_live_playlist_url(course_id, lesson_id, quality)
            content, _ = classroom_client.download_live_media(source_url, max_bytes=2 * 1024 * 1024)
            playlist = rewrite_live_playlist(content.decode("utf-8", errors="replace"), source_url)
        except ClassroomClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=playlist,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/courses/{course_id}/lessons/{lesson_id}/live/ppt")
    def api_live_ppt(course_id: str, lesson_id: str, after_id: int = 0) -> dict[str, object]:
        try:
            slides = classroom_client.list_live_ppt_slides(
                course_id,
                lesson_id,
                after_id=max(0, after_id),
            )
        except ClassroomClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "slides": [
                {
                    "id": slide.id,
                    "image_url": f"/api/live/ppt-image?url={quote(slide.image_url, safe='')}",
                    "thumbnail_url": (
                        f"/api/live/ppt-image?url={quote(slide.thumbnail_url, safe='')}"
                        if slide.thumbnail_url
                        else ""
                    ),
                    "created_at": slide.created_at,
                    "offset_seconds": slide.offset_seconds,
                }
                for slide in slides
            ],
            "last_id": max((slide.id for slide in slides), default=max(0, after_id)),
        }

    @app.get("/api/live/media")
    def api_live_media(url: str) -> Response:
        try:
            content, content_type = classroom_client.download_live_media(url)
            if "mpegurl" in content_type.lower() or url.split("?", 1)[0].lower().endswith(".m3u8"):
                content = rewrite_live_playlist(
                    content.decode("utf-8", errors="replace"),
                    url,
                ).encode("utf-8")
                content_type = "application/vnd.apple.mpegurl"
        except ClassroomClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=content,
            media_type=content_type.split(";", 1)[0],
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/live/ppt-image")
    def api_live_ppt_image(url: str) -> Response:
        try:
            content, content_type = classroom_client.download_live_ppt_image(url)
        except ClassroomClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=content,
            media_type=content_type.split(";", 1)[0],
            headers={"Cache-Control": "private, max-age=10"},
        )

    @app.get("/api/courses/{course_id}")
    def api_course_detail(course_id: str) -> dict[str, object]:
        detail = enrich_live_status(classroom_client.get_course_detail(course_id))
        return {
            "course": detail.course.__dict__,
            "lessons": [
                {
                    **lesson.__dict__,
                    "resources": [resource.__dict__ for resource in lesson.resources],
                }
                for lesson in detail.lessons
            ],
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
