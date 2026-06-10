from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import LoginSessionService
from .classroom import ClassroomClient, ClassroomClientError
from .config import PROJECT_ROOT, AppConfig


templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))


def create_app(config: AppConfig | None = None) -> FastAPI:
    app_config = config or AppConfig()
    auth_service = LoginSessionService(app_config)
    classroom_client = ClassroomClient(app_config)
    app = FastAPI(title="Fuck Classroom")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
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
        return RedirectResponse("/", status_code=303)

    @app.post("/auth/verify")
    def verify_login() -> RedirectResponse:
        auth_service.verify_saved_session()
        return RedirectResponse("/", status_code=303)

    @app.post("/auth/logout")
    def logout() -> RedirectResponse:
        auth_service.clear_session()
        return RedirectResponse("/", status_code=303)

    @app.get("/courses", response_class=HTMLResponse)
    def courses(request: Request) -> HTMLResponse:
        error = None
        courses = []
        try:
            courses = classroom_client.list_courses()
        except ClassroomClientError as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request,
            "courses.html",
            {
                "session": auth_service.get_session_status(),
                "courses": courses,
                "error": error,
            },
        )

    @app.get("/courses/{course_id}", response_class=HTMLResponse)
    def course_detail(request: Request, course_id: str) -> HTMLResponse:
        error = None
        detail = None
        try:
            detail = classroom_client.get_course_detail(course_id)
        except ClassroomClientError as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request,
            "course_detail.html",
            {
                "detail": detail,
                "error": error,
            },
        )

    @app.get("/api/courses")
    def api_courses() -> dict[str, object]:
        courses = classroom_client.list_courses()
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
