from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import LoginSessionService
from .config import PROJECT_ROOT, AppConfig


templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))


def create_app(config: AppConfig | None = None) -> FastAPI:
    app_config = config or AppConfig()
    auth_service = LoginSessionService(app_config)
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

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
