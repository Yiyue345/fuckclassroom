from __future__ import annotations

import asyncio
import inspect
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ...auth.cas import LoginVerificationError
from ...auth.credentials import CredentialStoreError
from ...core.plugins import PluginContext
from ...core.tasks import cancel_registered_task
from ...web.responses import task_started_response
from ..manager import PluginManagerError
from ..registry_preferences import RegistryPreferences
from ..registry_client import PluginRegistryClient, PluginRegistryError
from ..rpc import PLUGIN_RPC_API_VERSION


def _snapshot_file(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _restore_file(path: Path, payload: bytes | None) -> None:
    if payload is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".rollback")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _registry_channels(include_beta: bool) -> tuple[str, ...]:
    return ("stable", "beta") if include_beta else ("stable",)


def _registry_location(include_beta: bool, **parameters: str) -> str:
    query = {"include_beta": "1"} if include_beta else {}
    query.update(parameters)
    return "/plugins/registry" + (f"?{urlencode(query)}" if query else "")


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
    registry_client = PluginRegistryClient(plugin_manager)
    registry_preferences = RegistryPreferences(plugin_manager.config.data_dir)

    def selected_registry_beta(requested: bool | None) -> bool:
        return registry_preferences.include_beta() if requested is None else requested
    plugin_reload = services.maybe("plugin_reload")
    plugin_reload_background = services.maybe("plugin_reload_background")
    plugin_reload_status = services.maybe("plugin_reload_status")

    async def request_plugin_reload() -> bool:
        if not callable(plugin_reload):
            return False
        result = plugin_reload()
        if inspect.isawaitable(result):
            result = await result
        return result is not False

    def request_plugin_reload_from_worker() -> bool:
        callback = plugin_reload_background if callable(plugin_reload_background) else plugin_reload
        if not callable(callback):
            return False
        result = callback()
        if inspect.isawaitable(result):
            result = asyncio.run(result)
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

    @router.get("/plugins/registry", response_class=HTMLResponse)
    def plugin_registry_page(request: Request, include_beta: bool | None = None) -> HTMLResponse:
        selected_beta = selected_registry_beta(include_beta)
        error = request.query_params.get("error")
        rows: list[dict[str, object]] = []
        try:
            rows = registry_client.catalog(
                current_enabled=plugin_registry.plugin_ids,
                channels=_registry_channels(selected_beta),
            )
        except (PluginRegistryError, PluginManagerError) as exc:
            error = str(exc)
        response = templates.TemplateResponse(
            request,
            "core/plugin_registry.html",
            {
                "registry_plugins": rows,
                "registry_url": registry_client.registry_url,
                "include_beta": selected_beta,
                "plugin_config_mutable": plugin_manager.mutable,
                "notice": request.query_params.get("notice"),
                "error": error,
            },
        )
        if include_beta is not None:
            try:
                registry_preferences.set_include_beta(selected_beta)
            except OSError as exc:
                raise HTTPException(
                    status_code=500, detail="无法保存插件市场频道偏好"
                ) from exc
        return response

    @router.get("/api/plugins/registry")
    def plugin_registry_api(request: Request, include_beta: bool | None = None) -> JSONResponse:
        selected_beta = selected_registry_beta(include_beta)
        try:
            rows = registry_client.catalog(
                current_enabled=plugin_registry.plugin_ids,
                channels=_registry_channels(selected_beta),
            )
        except (PluginRegistryError, PluginManagerError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=502)
        return JSONResponse(
            {
                "registry_url": registry_client.registry_url,
                "include_beta": selected_beta,
                "plugins": rows,
            }
        )

    @router.post("/plugins/registry/{plugin_id}/install")
    def install_registry_plugin(request: Request, plugin_id: str, include_beta: bool | None = None) -> Response:
        selected_beta = selected_registry_beta(include_beta)
        if not plugin_manager.mutable:
            message = "当前插件配置为只读，不能从 Registry 安装插件"
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": message}, status_code=409)
            return RedirectResponse(
                _registry_location(selected_beta, error=message),
                status_code=303,
            )
        try:
            plan = registry_client.resolve_install_plan(
                plugin_id,
                current_enabled=plugin_registry.plugin_ids,
                channels=_registry_channels(selected_beta),
            )
        except (PluginRegistryError, PluginManagerError) as exc:
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": str(exc)}, status_code=409)
            return RedirectResponse(
                _registry_location(selected_beta, error=str(exc)),
                status_code=303,
            )

        if plan.action not in {"install", "update"}:
            message = (
                "插件已是当前可安装的最新版本"
                if plan.action == "installed"
                else plan.compatibility_message or "当前没有可安装版本"
            )
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": message}, status_code=409)
            return RedirectResponse(
                _registry_location(selected_beta, error=message),
                status_code=303,
            )
        if plan.update_blocked:
            message = "更新前请先停用该插件及依赖它的插件，等待 Runtime 卸载完成"
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": message}, status_code=409)
            return RedirectResponse(
                _registry_location(selected_beta, error=message),
                status_code=303,
            )

        current_enabled = tuple(plugin_registry.plugin_ids)

        def worker(progress):
            descriptor, action, signed = registry_client.install(
                plugin_id,
                current_enabled=current_enabled,
                channels=_registry_channels(selected_beta),
                progress=progress,
            )
            verification = "SHA-256 + Ed25519" if signed else "SHA-256"
            action_text = "更新" if action == "update" else "安装"
            progress.log(
                f"{action_text}完成：{descriptor.id}@{descriptor.version}，完整性校验：{verification}"
            )
            return _registry_location(
                selected_beta,
                notice=f"{descriptor.name} v{descriptor.version} 已{action_text}；默认保持停用",
            )

        task = task_manager.start(
            f"{'更新' if plan.action == 'update' else '下载'}插件 · {plan.name}",
            worker,
        )
        return task_started_response(request, task, _registry_location(selected_beta))

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
            if plugin_id in plugin_manager.desired_enabled():
                reload_scheduled = request_plugin_reload_from_worker()
                if reload_scheduled:
                    progress(100, "依赖安装完成，正在自动应用插件")
                    return (
                        f"/plugins?notice="
                        f"{quote(f'{descriptor.name} 的依赖安装完成，已请求自动应用')}"
                    )
                return (
                    f"/plugins?restart=1&notice="
                    f"{quote(f'{descriptor.name} 的依赖安装完成，重启应用后生效')}"
                )
            return f"/plugins?notice={quote(f'{descriptor.name} 的依赖安装完成')}"

        task = task_manager.start(f"安装插件依赖 · {descriptor.name}", worker)
        return task_started_response(request, task, "/plugins")

    @router.delete("/api/plugins/{plugin_id}")
    def remove_plugin_api(plugin_id: str) -> JSONResponse:
        descriptor = plugin_manager.descriptor_map().get(plugin_id)
        if descriptor is None:
            return JSONResponse(
                {"detail": f"插件不存在：{plugin_id}"},
                status_code=404,
            )
        try:
            cleanup_warning = plugin_manager.remove(
                plugin_id,
                current_enabled=plugin_registry.plugin_ids,
            )
        except PluginManagerError as exc:
            return JSONResponse(
                {"detail": str(exc)},
                status_code=409,
            )
        return JSONResponse(
            {
                "ok": True,
                "action": "remove",
                "plugin_id": descriptor.id,
                "name": descriptor.name,
                "version": descriptor.version,
                "message": f"插件 {descriptor.name} 已移除",
                "cleanup_warning": cleanup_warning,
            }
        )

    @router.post("/plugins/{plugin_id}/remove")
    def remove_plugin(plugin_id: str) -> RedirectResponse:
        try:
            cleanup_warning = plugin_manager.remove(
                plugin_id,
                current_enabled=plugin_registry.plugin_ids,
            )
        except PluginManagerError as exc:
            return RedirectResponse(
                f"/plugins?error={quote(str(exc))}",
                status_code=303,
            )
        notice = "插件文件已删除"
        if cleanup_warning:
            notice += f"；{cleanup_warning}"
        return RedirectResponse(
            f"/plugins?notice={quote(notice)}",
            status_code=303,
        )

    @router.get("/about", response_class=HTMLResponse)
    def about(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "core/about.html",
            {
                "app_version": plugin_manager.app_version,
                "plugin_api_version": plugin_manager.api_version,
                "plugin_rpc_api_version": PLUGIN_RPC_API_VERSION,
            },
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

        try:
            settings_snapshot = _snapshot_file(settings_store.path)
            credential_snapshot = _snapshot_file(credential_store.path)
        except OSError as exc:
            return RedirectResponse(
                f"/settings?settings_error={quote(f'读取现有设置失败：{exc}')}",
                status_code=303,
            )

        try:
            updated = settings_store.update_from_form(form)
            credential_store.update_from_form(
                form.get("account_username", ""), form.get("account_password", "")
            )
        except (CredentialStoreError, OSError) as exc:
            rollback_errors: list[str] = []
            for path, snapshot in (
                (settings_store.path, settings_snapshot),
                (credential_store.path, credential_snapshot),
            ):
                try:
                    _restore_file(path, snapshot)
                except OSError as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            message = str(exc)
            if rollback_errors:
                message += "；回滚失败：" + "；".join(rollback_errors)
            parameter = (
                "credential_error"
                if isinstance(exc, CredentialStoreError)
                else "settings_error"
            )
            return RedirectResponse(
                f"/settings?{parameter}={quote(message)}",
                status_code=303,
            )

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
