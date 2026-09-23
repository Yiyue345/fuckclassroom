from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..core.plugins import PluginRegistry, ServiceContainer


PLUGIN_RPC_API_VERSION = 1
_RPC_METHOD_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
_STORAGE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_STORAGE_BYTES = 512 * 1024


class PluginRpcError(RuntimeError):
    code = "rpc_error"


class PluginRpcPermissionError(PluginRpcError):
    code = "permission_denied"


class PluginRpcMethodNotFound(PluginRpcError):
    code = "method_not_found"


class PluginRpcInvalidParams(PluginRpcError):
    code = "invalid_params"


@dataclass(frozen=True, slots=True)
class PluginRpcCallContext:
    plugin_id: str
    method: str


RpcHandler = Callable[[PluginRpcCallContext, Mapping[str, object]], object]
CleanupHandler = Callable[[str], object]


class PluginRpcRegistry:
    """Versioned, permission-checked RPC surface exposed to isolated plugins."""

    def __init__(self) -> None:
        self._methods: dict[str, RpcHandler] = {}
        self._cleanup_handlers: list[CleanupHandler] = []

    @property
    def api_version(self) -> int:
        return PLUGIN_RPC_API_VERSION

    @property
    def methods(self) -> tuple[str, ...]:
        return tuple(sorted(self._methods))

    def register(self, method: str, handler: RpcHandler) -> None:
        normalized = str(method or "").strip()
        if not _RPC_METHOD_RE.fullmatch(normalized):
            raise ValueError(f"invalid plugin RPC method: {method}")
        if normalized in self._methods:
            raise ValueError(f"duplicate plugin RPC method: {normalized}")
        self._methods[normalized] = handler

    def register_cleanup(self, handler: CleanupHandler) -> None:
        self._cleanup_handlers.append(handler)

    async def call(
        self,
        *,
        plugin_id: str,
        permissions: Iterable[str],
        method: str,
        params: object = None,
    ) -> object:
        normalized = str(method or "").strip()
        if normalized not in set(permissions):
            raise PluginRpcPermissionError(
                f"插件 {plugin_id} 未声明 RPC 权限：{normalized}"
            )
        handler = self._methods.get(normalized)
        if handler is None:
            raise PluginRpcMethodNotFound(f"RPC 方法不可用：{normalized}")
        if params is None:
            mapping: Mapping[str, object] = {}
        elif isinstance(params, dict):
            mapping = params
        else:
            raise PluginRpcInvalidParams("RPC params 必须是 JSON object")

        context = PluginRpcCallContext(plugin_id=plugin_id, method=normalized)
        if inspect.iscoroutinefunction(handler):
            result = await handler(context, mapping)
        else:
            result = await asyncio.to_thread(handler, context, mapping)
            if inspect.isawaitable(result):
                result = await result
        _assert_json_value(result)
        return result

    async def plugin_stopped(self, plugin_id: str) -> None:
        for handler in tuple(self._cleanup_handlers):
            try:
                if inspect.iscoroutinefunction(handler):
                    await handler(plugin_id)
                else:
                    result = await asyncio.to_thread(handler, plugin_id)
                    if inspect.isawaitable(result):
                        await result
            except Exception:
                # Plugin shutdown cleanup is best effort; the Runtime shutdown
                # path must continue even if one cleanup provider fails.
                continue


class PluginRpcStorage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()

    @staticmethod
    def _key(params: Mapping[str, object]) -> str:
        key = str(params.get("key") or "").strip()
        if not _STORAGE_KEY_RE.fullmatch(key):
            raise PluginRpcInvalidParams(
                "storage key 必须为 1-128 位字母、数字、_、-、.、:，且以字母或数字开头"
            )
        return key

    def _path(self, plugin_id: str) -> Path:
        return self.root / f"{plugin_id}.json"

    def _load(self, plugin_id: str) -> dict[str, object]:
        path = self._path(plugin_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save(self, plugin_id: str, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) > _MAX_STORAGE_BYTES:
            raise PluginRpcInvalidParams("插件 RPC 存储上限为 512 KiB")
        path = self._path(plugin_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(path)

    def get(self, context: PluginRpcCallContext, params: Mapping[str, object]) -> object:
        key = self._key(params)
        with self._lock:
            return self._load(context.plugin_id).get(key, params.get("default"))

    def set(self, context: PluginRpcCallContext, params: Mapping[str, object]) -> object:
        key = self._key(params)
        if "value" not in params:
            raise PluginRpcInvalidParams("core.storage.set 缺少 value")
        _assert_json_value(params["value"])
        with self._lock:
            payload = self._load(context.plugin_id)
            payload[key] = params["value"]
            self._save(context.plugin_id, payload)
        return {"ok": True}

    def delete(self, context: PluginRpcCallContext, params: Mapping[str, object]) -> object:
        key = self._key(params)
        with self._lock:
            payload = self._load(context.plugin_id)
            existed = key in payload
            payload.pop(key, None)
            self._save(context.plugin_id, payload)
        return {"deleted": existed}

    def list_keys(
        self, context: PluginRpcCallContext, _params: Mapping[str, object]
    ) -> object:
        with self._lock:
            return sorted(self._load(context.plugin_id))


class PluginRpcBuiltins:
    def __init__(
        self,
        *,
        services: ServiceContainer,
        plugin_registry: PluginRegistry,
        data_dir: Path,
    ) -> None:
        self.services = services
        self.plugin_registry = plugin_registry
        self.storage = PluginRpcStorage(data_dir / "plugin-rpc-storage")
        self._task_lock = threading.Lock()
        self._task_owners: dict[str, str] = {}

    def register(self, registry: PluginRpcRegistry) -> None:
        registry.register("core.settings.get", self.settings_get)
        registry.register("core.plugins.enabled", self.plugins_enabled)
        registry.register("core.plugins.has", self.plugins_has)
        registry.register("core.storage.get", self.storage.get)
        registry.register("core.storage.set", self.storage.set)
        registry.register("core.storage.delete", self.storage.delete)
        registry.register("core.storage.list", self.storage.list_keys)
        registry.register("core.tasks.create", self.tasks_create)
        registry.register("core.tasks.update", self.tasks_update)
        registry.register("core.tasks.log", self.tasks_log)
        registry.register("core.tasks.complete", self.tasks_complete)
        registry.register("core.tasks.fail", self.tasks_fail)
        registry.register("core.tasks.cancelled", self.tasks_cancelled)
        registry.register("core.tasks.status", self.tasks_status)
        registry.register("core.tasks.cancel_requested", self.tasks_cancel_requested)
        registry.register_cleanup(self.plugin_stopped)

        if self.services.has("auth_service"):
            registry.register("classroom.session.status", self.classroom_session_status)
            registry.register("internal.classroom.session.ensure", self.classroom_session_ensure)
        if self.services.has("classroom_client"):
            registry.register("classroom.terms.list", self.classroom_terms_list)
            registry.register("classroom.courses.list", self.classroom_courses_list)
            registry.register("classroom.course.get", self.classroom_course_get)

        if self.services.has("course_selection"):
            registry.register("internal.selection.session.ensure", self.selection_session_ensure)
            registry.register("internal.selection.access.info", self.selection_access_info)
        if self.services.has("hy2_proxy"):
            registry.register("internal.selection.hy2.ensure", self.selection_hy2_ensure)

    def settings_get(
        self, _context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        settings = self.services.get("settings_store").load().to_public_dict()
        key = str(params.get("key") or "").strip()
        if not key:
            return settings
        if key not in settings:
            raise PluginRpcInvalidParams(f"未知设置项：{key}")
        return settings[key]

    def plugins_enabled(
        self, _context: PluginRpcCallContext, _params: Mapping[str, object]
    ) -> object:
        return list(self.plugin_registry.plugin_ids)

    def plugins_has(
        self, _context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        plugin_id = str(params.get("plugin_id") or "").strip()
        if not plugin_id:
            raise PluginRpcInvalidParams("core.plugins.has 缺少 plugin_id")
        return self.plugin_registry.is_enabled(plugin_id)

    def _remember_task(self, plugin_id: str, task_id: str) -> None:
        with self._task_lock:
            self._task_owners[task_id] = plugin_id

    def _task_id(
        self, context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> str:
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            raise PluginRpcInvalidParams(f"{context.method} 缺少 task_id")
        with self._task_lock:
            owner = self._task_owners.get(task_id)
        if owner != context.plugin_id:
            raise PluginRpcPermissionError("只能操作当前插件通过 RPC 创建的任务")
        return task_id

    def tasks_create(
        self, context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        title = str(params.get("title") or "").strip()
        if not title:
            raise PluginRpcInvalidParams("core.tasks.create 缺少 title")
        state = self.services.get("task_manager").create_external(
            title[:120],
            auto_open_result=bool(params.get("auto_open_result", False)),
        )
        self._remember_task(context.plugin_id, state.id)
        return {"task_id": state.id, "status": state.status}

    def tasks_update(
        self, context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        task_id = self._task_id(context, params)
        state = self.services.get("task_manager").update_external(
            task_id,
            progress=_optional_int(params.get("progress")),
            message=_optional_text(params.get("message")),
            detail_progress=_optional_int(params.get("detail_progress")),
            detail_message=_optional_text(params.get("detail_message")),
            clear_detail=bool(params.get("clear_detail", False)),
        )
        return asdict(state)

    def tasks_log(
        self, context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        task_id = self._task_id(context, params)
        message = str(params.get("message") or "").strip()
        if not message:
            raise PluginRpcInvalidParams("core.tasks.log 缺少 message")
        self.services.get("task_manager").log_external(
            task_id,
            message,
            level=str(params.get("level") or "info"),
        )
        return {"ok": True}

    def tasks_complete(
        self, context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        task_id = self._task_id(context, params)
        state = self.services.get("task_manager").complete_external(
            task_id,
            result_url=_optional_text(params.get("result_url")),
            message=str(params.get("message") or "任务完成"),
        )
        return asdict(state)

    def tasks_fail(
        self, context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        task_id = self._task_id(context, params)
        error = str(params.get("error") or "").strip()
        if not error:
            raise PluginRpcInvalidParams("core.tasks.fail 缺少 error")
        state = self.services.get("task_manager").fail_external(task_id, error)
        return asdict(state)

    def tasks_cancelled(
        self, context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        task_id = self._task_id(context, params)
        state = self.services.get("task_manager").cancel_external(task_id)
        return asdict(state)

    def tasks_status(
        self, context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        task_id = self._task_id(context, params)
        state = self.services.get("task_manager").as_dict(task_id)
        if state is None:
            raise PluginRpcInvalidParams("任务不存在")
        return state

    def tasks_cancel_requested(
        self, context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        task_id = self._task_id(context, params)
        return self.services.get("task_manager").is_cancel_requested(task_id)

    def plugin_stopped(self, plugin_id: str) -> None:
        manager = self.services.get("task_manager")
        with self._task_lock:
            task_ids = [
                task_id for task_id, owner in self._task_owners.items()
                if owner == plugin_id
            ]
        for task_id in task_ids:
            state = manager.get(task_id)
            if state is not None and state.status == "running":
                manager.fail_external(task_id, "插件进程已停止")

    def classroom_session_status(
        self, _context: PluginRpcCallContext, _params: Mapping[str, object]
    ) -> object:
        return asdict(self.services.get("auth_service").get_session_status())

    def classroom_session_ensure(
        self, _context: PluginRpcCallContext, _params: Mapping[str, object]
    ) -> object:
        return asdict(
            self.services.get("auth_service").verify_saved_session(auto_relogin=True)
        )

    def selection_session_ensure(
        self, _context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        selection = self.services.get("course_selection")
        status = selection.verify_saved_session(
            timeout_ms=_bounded_int(params.get("timeout_ms"), 15_000, 1_000, 600_000),
            auto_relogin=bool(params.get("auto_relogin", True)),
        )
        return asdict(status)

    def selection_access_info(
        self, _context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        return self.services.get("course_selection").get_access_info(
            force=bool(params.get("force", False))
        )

    def selection_hy2_ensure(
        self, _context: PluginRpcCallContext, _params: Mapping[str, object]
    ) -> object:
        return self.services.get("hy2_proxy").ensure_started()

    def classroom_terms_list(
        self, _context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        client = self.services.get("classroom_client")
        force = bool(params.get("force_refresh", False))
        terms = client.refresh_terms() if force else client.list_terms()
        return [_term_dto(item) for item in terms]

    def classroom_courses_list(
        self, _context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        page = _bounded_int(params.get("page"), 1, 1, 10000)
        page_size = _bounded_int(params.get("page_size"), 50, 1, 200)
        term_id = str(params.get("term_id") or "").strip() or None
        courses = self.services.get("classroom_client").list_courses(
            page=page,
            page_size=page_size,
            term_id=term_id,
            force_refresh=bool(params.get("force_refresh", False)),
        )
        return [_course_dto(item) for item in courses]

    def classroom_course_get(
        self, _context: PluginRpcCallContext, params: Mapping[str, object]
    ) -> object:
        course_id = str(params.get("course_id") or "").strip()
        if not course_id:
            raise PluginRpcInvalidParams("classroom.course.get 缺少 course_id")
        client = self.services.get("classroom_client")
        detail = (
            client.refresh_course_detail(course_id)
            if bool(params.get("force_refresh", False))
            else client.get_course_detail(course_id)
        )
        return {
            "course": _course_dto(detail.course),
            "lessons": [_lesson_dto(item) for item in detail.lessons],
        }


def register_builtin_rpc_methods(
    registry: PluginRpcRegistry,
    *,
    services: ServiceContainer,
    plugin_registry: PluginRegistry,
    data_dir: Path,
) -> PluginRpcBuiltins:
    provider = PluginRpcBuiltins(
        services=services,
        plugin_registry=plugin_registry,
        data_dir=data_dir,
    )
    provider.register(registry)
    return provider


def _assert_json_value(value: object) -> None:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise PluginRpcError("RPC 返回值必须可 JSON 序列化") from exc


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise PluginRpcInvalidParams("进度字段必须是整数") from exc


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _bounded_int(value: object, fallback: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, parsed))


def _term_dto(term: Any) -> dict[str, object]:
    return {
        "id": term.id,
        "name": term.name,
        "year": term.year,
        "season": term.season,
        "begin_date": term.begin_date,
        "end_date": term.end_date,
        "current": bool(term.current),
    }


def _course_dto(course: Any) -> dict[str, object]:
    return {
        "id": course.id,
        "title": course.title,
        "teacher": course.teacher,
        "type": course.type,
        "model": course.model,
        "start_at": course.start_at,
        "end_at": course.end_at,
        "lesson_count": course.lesson_count,
    }


def _lesson_dto(lesson: Any) -> dict[str, object]:
    return {
        "id": lesson.id,
        "title": lesson.title,
        "status_label": lesson.status_label,
        "start_at": lesson.start_at,
        "end_at": lesson.end_at,
        "duration_seconds": lesson.duration_seconds,
        "lecturer": lesson.lecturer,
        "is_live": bool(lesson.is_live),
        "room_name": lesson.room_name,
        "resources": [
            {
                "kind": resource.kind,
                "title": resource.title,
                "file_type": resource.file_type,
                "status": resource.status,
                "is_downloadable": bool(resource.is_downloadable),
            }
            for resource in lesson.resources
        ],
    }


__all__ = [
    "PLUGIN_RPC_API_VERSION",
    "PluginRpcBuiltins",
    "PluginRpcCallContext",
    "PluginRpcError",
    "PluginRpcInvalidParams",
    "PluginRpcMethodNotFound",
    "PluginRpcPermissionError",
    "PluginRpcRegistry",
    "PluginRpcStorage",
    "register_builtin_rpc_methods",
]
