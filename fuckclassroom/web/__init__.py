"""FastAPI application entry point."""

from fastapi import HTTPException

from ..core.tasks import cancel_registered_task
from .app import _unified_credential_path, create_app as _create_app
from .qr_assistant import register_qr_assistant_routes


def create_app(*args, **kwargs):
    app = _create_app(*args, **kwargs)
    config = kwargs.get("config") if "config" in kwargs else (args[0] if args else None)
    if app.state.plugin_registry.is_enabled("classroom"):
        register_qr_assistant_routes(
            app,
            config,
            classroom_client=app.state.classroom_client,
            ketangpai_client=app.state.ketangpai_client,
        )

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str) -> dict[str, object]:
        try:
            return cancel_registered_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    return app


__all__ = ["create_app", "_unified_credential_path"]
