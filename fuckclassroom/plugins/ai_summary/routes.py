from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ...web.responses import task_started_response


def build_router(context) -> APIRouter:
    services = context.services
    router = APIRouter()
    task_manager = services.get("task_manager")
    extraction_service = services.get("extraction_service")

    @router.post("/courses/{course_id}/lessons/{lesson_id}/summarize")
    def summarize_lesson(request: Request, course_id: str, lesson_id: str) -> Response:
        result_url = f"/courses/{course_id}/lessons/{lesson_id}/outputs"

        def worker(progress):
            extraction_service.summarize_lesson(
                course_id,
                lesson_id,
                progress=progress,
            )
            return result_url

        task = task_manager.start("生成 AI 总结", worker)
        return task_started_response(request, task, result_url)

    return router


__all__ = ["build_router"]
