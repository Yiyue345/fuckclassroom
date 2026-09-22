from __future__ import annotations

import inspect
import tempfile
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ...auth.cas import LoginVerificationError
from ...auth.credentials import CredentialStoreError
from ...core.plugins import PluginContext
from ...core.tasks import cancel_registered_task
from ...web.responses import task_started_response
from ..manager import PluginManagerError


def build_router(context: PluginContext) -> APIRouter:
    router = APIRouter()
    plugin_registry = context.registry
    services = context.services

    templates = services.get("templates")
    settings_store = services.get("settings_store")
    cache_store = services.get("cache_store")
    task_manager = services.get("task_manager")
    credential_store = services.get("credential_store")
    verification_broker = services.get("verification_broker")
    plugin_manager = services.get("plugin_manager")
    plugin_reload = services.maybe("plugin_reload")
    plugin_reload_status = services.maybe("plugin_reload_status")

    async def request_plugin_reload() -> bool:
        if not callable(plugin_reload):
            return False
        result = plugin_reload()
        if inspect.isawaitable(result):
            result = await result
        return result is not False

    def reload_status() -> dict[str, object]:
        if not callable(plugin_reload_status):
            return {}
        try:
            value = plugin_reload_status()
        except Exception:  # noqa: BLE001 - status rendering must stay available.
            return {}
        return value if isinstance(value, dict) else {}

    @router.get("/")
    def index() -> RedirectResponse:
        if plugin_registry.is_enabled("classroom"):
            return RedirectResponse("/classroom", status_code=307)
        if plugin_registry.is_enabled("course_selection"):
            return RedirectResponse("/selection/info", status_code=307)
        return RedirectResponse("/settings", status_code=307)

    @router.get("/api/auth/challenge")
    def current_auth_challenge() -> JSONResponse:
        return JSONResponse({"challenge": verification_broker.current()})

    @router.post("/api/auth/challenge/{challenge_id}/send")
    def send_auth_code(challenge_id: str) -> JSONResponse:
        try:
            verification_broker.request_send(challenge_id)
        except LoginVerificationError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return JSONResponse({"ok": True})

    @router.post("/api/auth/challenge/{challenge_id}/verify")
    async def verify_auth_code(challenge_id: str, request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            verification_broker.request_verify(challenge_id, str(payload.get("code") or ""))
        except (ValueError, AttributeError, LoginVerificationError) as exc:
            return JSONResponse({"detail": str(exc) or "验证码请求无效"}, status_code=409)
        return JSONResponse({"ok": True})

    @router.post("/api/auth/challenge/{challenge_id}/cancel")
    def cancel_auth_code(challenge_id: str) -> JSONResponse:
        try:
            verification_broker.request_cancel(challenge_id)
        except LoginVerificationError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return JSONResponse({"ok": True})
    @router.get("/plugins", response_class=HTMLResponse)
    def plugins_page(request: Request) -> HTMLResponse:
        error = request.query_params.get("error")
        rows = []
        try:
            rows = plugin_manager.status_rows(plugin_registry.plugin_ids)
        except PluginManagerError as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request,
            "core/plugin_manager.html",
            {
                "plugins": rows,
                "selection_source": plugin_manager.selection_source,
                "plugin_config_mutable": plugin_manager.mutable,
                "plugin_dir": plugin_manager.plugin_dir,
                "plugin_api_version": plugin_manager.api_version,
                "app_version": plugin_manager.app_version,
                "notice": request.query_params.get("notice"),
                "error": error,
                "hot_reload_available": callable(plugin_reload),
                "reload_status": reload_status(),
                "restart_required": request.query_params.get("restart") == "1"
                    or any(bool(item.get("restart_required")) for item in rows),
            },
        )

    def plugin_action_response(
        request: Request,
        *,
        plugin_id: str,
        action: str,
        desired: set[str],
        reload_scheduled: bool,
    ) -> Response:
        message = (
            "已保存启用配置并自动应用"
            if action == "enable" and reload_scheduled
            else "已保存停用配置并自动应用；依赖插件也已同步处理"
            if action == "disable" and reload_scheduled
            else "已保存启用配置，重启应用后生效"
            if action == "enable"
            else "已保存停用配置；依赖该插件的功能也会在重启后停用"
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {
                    "ok": True,
                    "plugin_id": plugin_id,
                    "action": action,
                    "desired_enabled": sorted(desired),
                    "reload_scheduled": reload_scheduled,
                    "message": message,
                }
            )
        return RedirectResponse(
            f"/plugins?{'notice' if reload_scheduled else 'restart=1&notice'}={quote(message)}",
            status_code=303,
        )

    @router.post("/plugins/{plugin_id}/enable")
    async def enable_plugin(plugin_id: str, request: Request) -> Response:
        try:
            desired = plugin_manager.enable(plugin_id)
        except PluginManagerError as exc:
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": str(exc)}, status_code=409)
            return RedirectResponse(
                f"/plugins?error={quote(str(exc))}",
                status_code=303,
            )
        reload_scheduled = await request_plugin_reload()
        return plugin_action_response(
            request,
            plugin_id=plugin_id,
            action="enable",
            desired=desired,
            reload_scheduled=reload_scheduled,
        )

    @router.post("/plugins/{plugin_id}/disable")
    async def disable_plugin(plugin_id: str, request: Request) -> Response:
        try:
            desired = plugin_manager.disable(plugin_id)
        except PluginManagerError as exc:
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": str(exc)}, status_code=409)
            return RedirectResponse(
                f"/plugins?error={quote(str(exc))}",
                status_code=303,
            )
        reload_scheduled = await request_plugin_reload()
        return plugin_action_response(
            request,
            plugin_id=plugin_id,
            action="disable",
            desired=desired,
            reload_scheduled=reload_scheduled,
        )

    @router.get("/api/plugins/runtime-status")
    def plugin_runtime_status() -> JSONResponse:
        rows = plugin_manager.status_rows(plugin_registry.plugin_ids)
        status = reload_status()
        return JSONResponse(
            {
                "hot_reload_available": callable(plugin_reload),
                "restart_required": any(bool(item.get("restart_required")) for item in rows),
                "current_enabled": list(plugin_registry.plugin_ids),
                "desired_enabled": sorted(plugin_manager.desired_enabled()),
                "reloading": bool(status.get("reloading")),
                "last_error": str(status.get("last_error") or ""),
            }
        )

    @router.post("/plugins/install")
    async def install_plugin(request: Request) -> RedirectResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        source_path = (parsed.get("source_path") or [""])[-1]
        try:
            descriptor = plugin_manager.install_from_path(source_path)
        except PluginManagerError as exc:
            return RedirectResponse(
                f"/plugins?error={quote(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(
            f"/plugins?notice={quote(f'插件 {descriptor.name} 已添加，默认保持停用')}",
            status_code=303,
        )

    @router.post("/plugins/install-upload")
    async def install_plugin_upload(request: Request) -> JSONResponse:
        max_bytes = 50 * 1024 * 1024
        try:
            content_length = int(request.headers.get("content-length") or 0)
        except ValueError:
            content_length = 0
        if content_length > max_bytes:
            return JSONResponse({"detail": "插件包不能超过 50 MB"}, status_code=413)
        payload = await request.body()
        if not payload:
            return JSONResponse({"detail": "插件包为空"}, status_code=400)
        if len(payload) > max_bytes:
            return JSONResponse({"detail": "插件包不能超过 50 MB"}, status_code=413)

        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as temporary:
                temporary.write(payload)
                temporary_path = temporary.name
            descriptor = plugin_manager.install_from_path(temporary_path)
        except PluginManagerError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        finally:
            if temporary_path:
                try:
                    from pathlib import Path

                    Path(temporary_path).unlink(missing_ok=True)
                except OSError:
                    pass
        return JSONResponse(
            {
                "ok": True,
                "plugin_id": descriptor.id,
                "name": descriptor.name,
                "message": f"插件 {descriptor.name} 已添加，默认保持停用",
            }
        )

    @router.post("/plugins/{plugin_id}/dependencies/install")
    def install_plugin_dependencies(
        request: Request,
        plugin_id: str,
    ) -> Response:
        try:
            descriptor = plugin_manager.descriptor_map().get(plugin_id)
        except PluginManagerError as exc:
            return RedirectResponse(
                f"/plugins?error={quote(str(exc))}",
                status_code=303,
            )
        if descriptor is None:
            return RedirectResponse(
                f"/plugins?error={quote(f'插件不存在：{plugin_id}')}",
                status_code=303,
            )

        def worker(progress):
            plugin_manager.install_dependencies(plugin_id, progress)
            return f"/plugins?notice={quote(f'{descriptor.name} 的依赖安装完成')}"

        task = task_manager.start(f"安装插件依赖 · {descriptor.name}", worker)
        return task_started_response(request, task, "/plugins")

    @router.post("/plugins/{plugin_id}/remove")
    def remove_plugin(plugin_id: str) -> RedirectResponse:
        try:
            plugin_manager.remove(
                plugin_id,
                current_enabled=plugin_registry.plugin_ids,
            )
        except PluginManagerError as exc:
            return RedirectResponse(
                f"/plugins?error={quote(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(
            f"/plugins?notice={quote('插件文件已删除')}",
            status_code=303,
        )

    @router.get("/settings", response_class=HTMLResponse)
    def settings(request: Request) -> HTMLResponse:
        panels = []
        for panel in plugin_registry.settings_panels:
            panel_context = (
                panel.context_factory(services, request)
                if panel.context_factory is not None
                else {}
            )
            panels.append({"panel": panel, "context": panel_context})
        return templates.TemplateResponse(
            request,
            "core/settings.html",
            {
                "settings": settings_store.load().to_public_dict(),
                "settings_panels": panels,
                "saved": request.query_params.get("saved") == "1",
                "settings_error": request.query_params.get("settings_error"),
                "cache_cleared": request.query_params.get("cache_cleared") == "1",
                "cache_error": request.query_params.get("cache_error"),
                "credential_error": request.query_params.get("credential_error"),
                "credentials_cleared": request.query_params.get("credentials_cleared") == "1",
                "login_state_cleared": request.query_params.get("login_state_cleared") == "1",
                "login_state_error": request.query_params.get("login_state_error"),
            },
        )

    @router.post("/settings")
    async def save_settings(request: Request) -> RedirectResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        form = {key: values[-1] if values else "" for key, values in parsed.items()}
        for field in plugin_registry.active_checkbox_fields:
            form.setdefault(field, "")
        updated = settings_store.update_from_form(form)
        try:
            credential_store.update_from_form(
                form.get("account_username", ""), form.get("account_password", "")
            )
        except CredentialStoreError as exc:
            return RedirectResponse(f"/settings?credential_error={quote(str(exc))}", status_code=303)
        hook_errors = await context.notify_settings_saved(updated, form)
        if hook_errors:
            return RedirectResponse(
                f"/settings?saved=1&settings_error={quote('; '.join(hook_errors))}",
                status_code=303,
            )
        return RedirectResponse("/settings?saved=1", status_code=303)

    @router.post("/settings/credentials/clear")
    def clear_selection_credentials() -> RedirectResponse:
        try:
            credential_store.clear()
        except CredentialStoreError as exc:
            return RedirectResponse(f"/settings?credential_error={quote(str(exc))}", status_code=303)
        return RedirectResponse("/settings?credentials_cleared=1", status_code=303)

    @router.post("/settings/login-state/clear")
    async def clear_login_state() -> RedirectResponse:
        errors = await context.clear_plugin_sessions()
        if errors:
            return RedirectResponse(
                f"/settings?login_state_error={quote('; '.join(errors))}",
                status_code=303,
            )
        return RedirectResponse("/settings?login_state_cleared=1", status_code=303)

    @router.post("/settings/cache/clear")
    def clear_cache() -> RedirectResponse:
        try:
            cache_store.clear()
        except OSError as exc:
            return RedirectResponse(f"/settings?cache_error={quote(str(exc))}", status_code=303)
        return RedirectResponse("/settings?cache_cleared=1", status_code=303)

    @router.get("/accounts", response_class=HTMLResponse)
    def accounts(request: Request) -> HTMLResponse:
        panels = []
        for panel in plugin_registry.account_panels:
            panel_context = (
                panel.context_factory(services, request)
                if panel.context_factory is not None
                else {}
            )
            panels.append({"panel": panel, "context": panel_context})
        return templates.TemplateResponse(
            request,
            "core/accounts.html",
            {
                "account_panels": panels,
            },
        )

    @router.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_status(request: Request, task_id: str) -> HTMLResponse:
        task = task_manager.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return templates.TemplateResponse(
            request,
            "core/task_status.html",
            {
                "task": task,
            },
        )

    @router.get("/tasks", response_class=HTMLResponse)
    def tasks_overview(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "core/tasks.html",
            {
                "tasks": task_manager.list_recent(),
            },
        )

    @router.get("/api/tasks/{task_id}")
    def api_task_status(task_id: str) -> dict[str, object]:
        task = task_manager.as_dict(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task

    @router.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str) -> dict[str, object]:
        try:
            return cancel_registered_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return router


__all__ = ["build_router"]
