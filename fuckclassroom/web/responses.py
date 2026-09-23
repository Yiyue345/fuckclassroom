from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response


def task_started_response(
    request: Request,
    task,
    result_url: str | None = None,
) -> Response:
    if "application/json" in request.headers.get("accept", ""):
        payload = {
            "task_id": task.id,
            "status_url": f"/api/tasks/{task.id}",
        }
        if result_url:
            payload["result_url"] = result_url
        return JSONResponse(payload)
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


__all__ = ["task_started_response"]
