"""FastAPI application entry point."""

from fastapi import HTTPException

from ..core.tasks import cancel_registered_task
from .app import _unified_credential_path, create_app as _create_app


def create_app(*args, **kwargs):
    app = _create_app(*args, **kwargs)

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str) -> dict[str, object]:
        try:
            return cancel_registered_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    return app


__all__ = ["create_app", "_unified_credential_path"]
