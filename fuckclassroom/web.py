from __future__ import annotations

from urllib.parse import parse_qs
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .auth import LoginSessionService
from .cache import JsonCache
from .classroom import ClassroomClient, ClassroomClientError
from .config import PROJECT_ROOT, AppConfig
from .course_selection import CourseSelectionAssistant, SelectionTarget
from .downloads import DownloadLibrary
from .extractor import ExtractionError, LessonExtractionService
from .settings import SettingsStore
from .tasks import TaskManager


templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))


def create_app(config: AppConfig | None = None) -> FastAPI:
    app_config = config or AppConfig()
    auth_service = LoginSessionService(app_config)
    classroom_client = ClassroomClient(app_config)
    course_selection = CourseSelectionAssistant(app_config)
    extraction_service = LessonExtractionService(app_config)
    settings_store = SettingsStore(app_config.data_dir / "settings.json")
    cache_store = JsonCache(app_config.cache_dir)
    download_library = DownloadLibrary(app_config.downloads_dir)
    task_manager = TaskManager()
    app = FastAPI(title="Fuck Classroom")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "home.html", {})

    @app.get("/classroom", response_class=HTMLResponse)
    def classroom_home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "session": auth_service.get_session_status(),
                "login_run": auth_service.get_login_run_status(),
            },
        )

    @app.post("/auth/start")
    def start_login(background_tasks: BackgroundTasks) -> RedirectResponse:
        auth_service.mark_login_started()
        background_tasks.add_task(auth_service.start_browser_login)
        return RedirectResponse("/classroom", status_code=303)

    @app.post("/auth/verify")
    def verify_login() -> RedirectResponse:
        auth_service.verify_saved_session()
        return RedirectResponse("/classroom", status_code=303)

    @app.post("/auth/logout")
    def logout() -> RedirectResponse:
        auth_service.clear_session()
        return RedirectResponse("/classroom", status_code=303)

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
                "settings": settings_store.load().to_public_dict(),
                "terms": terms,
                "saved": request.query_params.get("saved") == "1",
                "cache_cleared": request.query_params.get("cache_cleared") == "1",
                "cache_error": request.query_params.get("cache_error"),
                "cache_info": cache_store.info(),
                "error": error,
            },
        )

    @app.post("/settings")
    async def save_settings(request: Request) -> RedirectResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        form = {key: values[-1] if values else "" for key, values in parsed.items()}
        settings_store.update_from_form(form)
        return RedirectResponse("/settings?saved=1", status_code=303)

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
            detail = classroom_client.get_course_detail(course_id)
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
                "detail": detail,
                "downloaded_exports": downloaded_exports,
                "error": error,
            },
        )

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
                "select_error": request.query_params.get("select_error"),
            },
        )

    @app.get("/selection/info", response_class=HTMLResponse)
    def selection_info(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "selection_info.html",
            {
                "session": course_selection.get_session_status(),
                "info": course_selection.get_selection_info(),
            },
        )

    @app.post("/selection/info/refresh")
    def refresh_selection_info() -> RedirectResponse:
        task = task_manager.start(
            "刷新选课信息",
            lambda progress: course_selection.refresh_selection_info_for_web(progress=progress),
        )
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @app.post("/selection/login")
    def start_webvpn_login() -> RedirectResponse:
        task = task_manager.start(
            "WebVPN 登录",
            lambda progress: course_selection.login_for_web(progress=progress),
        )
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @app.post("/selection/open")
    def open_selection_home() -> RedirectResponse:
        task = task_manager.start(
            "打开本科教务",
            lambda progress: course_selection.open_home_for_web(progress=progress),
        )
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @app.post("/selection/open-course-select")
    def open_course_select() -> RedirectResponse:
        task = task_manager.start(
            "打开选课入口",
            lambda progress: course_selection.open_course_select_for_web(progress=progress),
        )
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @app.post("/selection/capture")
    async def capture_selection_interfaces(request: Request) -> RedirectResponse:
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
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @app.get("/selection/captures", response_class=HTMLResponse)
    def selection_captures(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "selection_captures.html",
            {
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
                "name": name,
                "content": content,
            },
        )

    @app.post("/selection/highlight")
    async def highlight_selection_course(request: Request) -> RedirectResponse:
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
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @app.post("/selection/select-once")
    async def select_course_once(request: Request) -> RedirectResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        if (parsed.get("confirm_select") or [""])[-1] != "YES":
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
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @app.post("/courses/{course_id}/lessons/{lesson_id}/extract")
    def extract_lesson(course_id: str, lesson_id: str) -> RedirectResponse:
        result_url = f"/courses/{course_id}/lessons/{lesson_id}/outputs"

        def worker(progress):
            extraction_service.extract_lesson(course_id, lesson_id, run_ocr=True, progress=progress)
            return result_url

        task = task_manager.start("提取文字", worker)
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @app.post("/courses/{course_id}/lessons/{lesson_id}/summarize")
    def summarize_lesson(course_id: str, lesson_id: str) -> RedirectResponse:
        result_url = f"/courses/{course_id}/lessons/{lesson_id}/outputs"

        def worker(progress):
            extraction_service.summarize_lesson(course_id, lesson_id, progress=progress)
            return result_url

        task = task_manager.start("生成 AI 总结", worker)
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_status(request: Request, task_id: str) -> HTMLResponse:
        task = task_manager.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return templates.TemplateResponse(
            request,
            "task_status.html",
            {
                "task": task,
                "auto_open_outputs": app_config.auto_open_outputs_after_extract and task.title == "提取文字",
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
