from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ...web.responses import task_started_response
from ...course_selection.models import CourseSelectionApiError


class SelectionRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=100)
    group_id: str = Field(default="", max_length=100)
    subject_id: str = Field(default="", max_length=100)
    item_ids: str = Field(default="", max_length=4000)


class WaitlistRequest(SelectionRequest):
    label: str = Field(default="", max_length=250)
    interval: int = Field(default=30, ge=15, le=3600)


def build_router(context):
    router = APIRouter()
    services = context.services
    lab = services.get("lab_selection")
    waitlist = services.get("lab_waitlist")

    def same_origin(request):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="请从本机实验课页面提交")

    @router.get("/lab-selection")
    def index(request: Request):
        with lab.session.defer_hy2_start():
            status = lab.session.get_session_status()
        return services.get("templates").TemplateResponse(
            request, "lab_selection/index.html", {"session": status},
        )

    @router.post("/lab-selection/login")
    def login(request: Request):
        task = services.get("task_manager").start("登录本科教务", lab.login)
        return task_started_response(request, task, "/lab-selection")

    def invoke(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (CourseSelectionApiError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/lab-selection/courses")
    def courses(semester: str = Query(default="", max_length=100), page: int = Query(default=1, ge=1, le=10000)):
        return invoke(lab.courses, semester, page)

    @router.get("/api/lab-selection/subjects")
    def subjects(task_id: str = Query(min_length=1, max_length=100)):
        return invoke(lab.subjects, task_id)

    @router.get("/api/lab-selection/slots")
    def slots(task_id: str = Query(min_length=1, max_length=100), subject_id: str = Query(min_length=1, max_length=100)):
        return invoke(lab.slots, task_id, subject_id)

    @router.post("/api/lab-selection/select")
    def select(request: Request, body: SelectionRequest):
        same_origin(request)
        return invoke(lab.select, **body.model_dump())

    @router.get("/api/lab-selection/waitlist")
    def list_waitlist():
        return waitlist.list_jobs()

    @router.post("/api/lab-selection/waitlist")
    def add_waitlist(request: Request, body: WaitlistRequest):
        same_origin(request)
        return invoke(waitlist.add, **body.model_dump())

    @router.post("/api/lab-selection/waitlist/{job_id}/cancel")
    def cancel_waitlist(request: Request, job_id: str):
        same_origin(request)
        invoke(waitlist.cancel, job_id)
        return {"message": "已取消后续尝试，已发出的请求仍会核验结果"}

    return router
