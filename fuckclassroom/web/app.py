from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..core.config import PROJECT_ROOT, AppConfig
from ..core.plugins import PluginRuntime
from ..core.tasks import TaskManager
from ..plugins import PluginManager, build_plugin_registry
from ..plugins.rpc import PluginRpcRegistry, register_builtin_rpc_methods


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


def create_app(
    config: AppConfig | None = None,
    enabled_plugins: Iterable[str] | None = None,
    *,
    reload_callback: Callable[[], object] | None = None,
    reload_status: Callable[[], dict[str, object]] | None = None,
    shared_task_manager: TaskManager | None = None,
) -> FastAPI:
    app_config = config or AppConfig()
    plugin_manager = PluginManager(app_config)
    plugin_registry = build_plugin_registry(
        enabled_plugins,
        config=app_config,
        manager=plugin_manager,
    )
    plugin_runtime = PluginRuntime(plugin_registry, app_config, logger)
    plugin_rpc = PluginRpcRegistry()
    plugin_runtime.services.add("plugin_manager", plugin_manager)
    plugin_runtime.services.add("plugin_rpc", plugin_rpc)
    if shared_task_manager is not None:
        plugin_runtime.services.add("task_manager", shared_task_manager)
    if reload_callback is not None:
        plugin_runtime.services.add("plugin_reload", reload_callback)
    if reload_status is not None:
        plugin_runtime.services.add("plugin_reload_status", reload_status)
    plugin_runtime.initialize_services()
    register_builtin_rpc_methods(
        plugin_rpc,
        services=plugin_runtime.services,
        plugin_registry=plugin_registry,
        data_dir=app_config.data_dir,
    )

    templates = Jinja2Templates(
        directory=plugin_registry.template_directories(PROJECT_ROOT / "templates")
    )
    plugin_runtime.services.add("templates", templates)
    plugin_runtime.initialize_routes()
    services = plugin_runtime.services

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await plugin_runtime.start()
        try:
            yield
        finally:
            await plugin_runtime.stop()

    app = FastAPI(title="Fuck Classroom", lifespan=lifespan)
    classroom_client = services.maybe("classroom_client")
    ketangpai_client = services.maybe("ketangpai_client")
    auth_service = services.maybe("auth_service")
    course_selection = services.maybe("course_selection")

    app.state.classroom_client = classroom_client
    app.state.ketangpai_client = ketangpai_client
    app.state.get_classroom_session_status = (
        auth_service.get_session_status
        if auth_service is not None
        else lambda: {"is_logged_in": False, "saved_at": ""}
    )
    app.state.get_selection_session_status = (
        course_selection.get_session_status
        if course_selection is not None
        else lambda: {"is_saved": False, "is_valid": False, "checked_at": ""}
    )
    app.state.semester_sync = services.maybe("semester_sync")
    app.state.plugin_registry = plugin_registry
    app.state.plugin_runtime = plugin_runtime
    app.state.plugin_manager = plugin_manager
    app.state.plugin_rpc = plugin_rpc

    app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")
    for mount in plugin_registry.static_mounts:
        app.mount(mount.path, StaticFiles(directory=str(mount.directory)), name=mount.name)

    plugin_runtime.include_routes(app)
    return app
