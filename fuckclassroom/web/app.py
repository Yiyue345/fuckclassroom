from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from urllib.parse import parse_qs
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..auth import LoginSessionService
from ..auth.cas import LoginVerificationBroker, LoginVerificationError
from ..auth.credentials import CredentialStoreError, SelectionCredentialStore
from ..classroom import ClassroomClient, ClassroomClientError
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
from ..course_selection.proxy import Hy2ProxyManager


templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))
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


def create_app(config: AppConfig | None = None) -> FastAPI:
    app_config = config or AppConfig()
    classroom_client = ClassroomClient(app_config)
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
        return auth_service.verify_saved_session()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def verify_saved_sessions() -> None:
            results = await asyncio.gather(
                asyncio.to_thread(verify_classroom_session),
                asyncio.to_thread(course_selection.verify_saved_session),
                return_exceptions=True,
            )
            labels = ("课程录播", "WebVPN/教务")
            for label, result in zip(labels, results):
                if isinstance(result, Exception):
                    logger.warning("启动时%s会话检测失败：%s", label, result)
                else:
                    logger.info("启动时%s会话状态：%s", label, result.message)

        startup_check = asyncio.create_task(verify_saved_sessions())
        auto_selection.start()
        try:
            yield
        finally:
            if not startup_check.done():
                startup_check.cancel()
            try:
                await startup_check
            except asyncio.CancelledError:
                pass
            auto_selection.stop()
            hy2_proxy.stop()

    app = FastAPI(title="Fuck Classroom", lifespan=lifespan)
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
        if session.is_logged_in:
            try:
                courses = classroom_client.list_courses(term_id=app_config.default_term_id or None)
            except ClassroomClientError as exc:
                course_error = str(exc)
        files = download_library.list_files()
        recent_tasks = task_manager.list_recent(4)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "session": session,
                "login_run": auth_service.get_login_run_status(),
                "courses": courses,
                "course_error": course_error,
                "files": files,
                "recent_tasks": recent_tasks,
                "running_task_count": sum(task.status == "running" for task in recent_tasks),
                "completed_task_count": sum(task.status == "succeeded" for task in recent_tasks),
            },
        )

    @app.post("/auth/start")
    def start_login(request: Request) -> Response:
        task = task_manager.start(
            "课程录播登录", lambda progress: auth_service.login_for_web(progress)
        )
        return task_started_response(request, task, "/classroom#session")

    @app.post("/auth/verify")
    def verify_login(request: Request) -> Response:
        task = task_manager.start(
            "验证课程录播会话", lambda progress: auth_service.verify_for_web(progress)
        )
        return task_started_response(request, task, "/classroom#session")

    @app.post("/auth/logout")
    def logout() -> RedirectResponse:
        auth_service.clear_session()
        return RedirectResponse("/classroom", status_code=303)

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
        selected_term_id = term_id if term_id is not None else app_config.default_term_id
        selected_term = None
        try:
            terms = classroom_client.list_terms()
            selected_term = next((item for item in terms if item.id == selected_term_id), None)
        except ClassroomClientError as exc:
            term_error = f"学期列表加载失败：{exc}"

        try:
            courses = classroom_client.list_courses(term=selected_term)
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
            terms = classroom_client.list_terms()
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
        downloaded_exports: list[str] = []
        try:
            detail = classroom_client.refresh_course_detail(course_id)
            for lesson in detail.lessons:
                for resource in lesson.resources:
                    if resource.kind not in {"ppt", "transcript"}:
                        continue
                    target = classroom_client.get_lesson_export_target(course_id, lesson.id, resource.kind)
                    if target.exists:
                        downloaded_exports.append(f"{lesson.id}:{resource.kind}")
        except ClassroomClientError as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request,
            "course_detail.html",
            {
                "session": auth_service.get_session_status(),
                "detail": detail,
                "course_id": course_id,
                "downloaded_exports": downloaded_exports,
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

    @app.get("/selection", response_class=HTMLResponse)
    def selection(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "selection.html",
            {
                "session": course_selection.get_session_status(),
                "access": course_selection.get_access_info(),
                "credentials": course_selection.get_credential_status(),
                "select_error": request.query_params.get("select_error"),
            },
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
        return templates.TemplateResponse(
            request,
            "selection_info.html",
            {
                "session": course_selection.get_session_status(),
                "info": course_selection.get_selection_info(view),
                "views": SELECTION_INFO_VIEWS,
                "last_action": course_selection.get_selection_action_result(),
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
        return JSONResponse(course_selection.get_selection_action_result() or {})

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
            lambda progress: course_selection.login_for_web(progress=progress, force_interactive=True),
        )
        return task_started_response(request, task, "/selection")

    @app.post("/selection/open")
    def open_selection_home(request: Request) -> Response:
        task = task_manager.start(
            "打开本科教务",
            lambda progress: course_selection.open_home_for_web(progress=progress),
        )
        return task_started_response(request, task, "/selection")

    @app.post("/selection/open-course-select")
    def open_course_select(request: Request) -> Response:
        task = task_manager.start(
            "打开选课入口",
            lambda progress: course_selection.open_course_select_for_web(progress=progress),
        )
        return task_started_response(request, task, "/selection")

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
            lambda progress: course_selection.capture_interfaces_for_web(
                progress=progress,
                duration_seconds=duration_seconds,
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

    @app.get("/api/courses/{course_id}")
    def api_course_detail(course_id: str) -> dict[str, object]:
        detail = classroom_client.get_course_detail(course_id)
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
