from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from fastapi import FastAPI

from ..core.config import AppConfig
from ..core.tasks import TaskManager
from .app import create_app


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Generation:
    app: FastAPI
    lifespan: Any
    active_requests: int = 0
    drained: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.drained.set()


class ReloadableApplication:
    """ASGI shell that swaps complete FastAPI generations in the same process."""

    def __init__(
        self,
        app_factory: Callable[[Callable[[], object], Callable[[], dict[str, object]]], FastAPI],
    ) -> None:
        self._app_factory = app_factory
        self._current: _Generation | None = None
        self._request_gate: asyncio.Event | None = None
        self._reload_task: asyncio.Task[None] | None = None
        self._startup_lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_reload_error = ""
        self._shutting_down = False

    def _primitives(self) -> tuple[asyncio.Event, asyncio.Lock]:
        if self._request_gate is None:
            self._request_gate = asyncio.Event()
        if self._startup_lock is None:
            self._startup_lock = asyncio.Lock()
        return self._request_gate, self._startup_lock

    async def _start_generation(self) -> _Generation:
        app = self._app_factory(self.request_reload_threadsafe, self.runtime_status)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        return _Generation(app=app, lifespan=lifespan)

    async def _stop_generation(self, generation: _Generation) -> None:
        try:
            await generation.lifespan.__aexit__(None, None, None)
        except Exception as exc:  # noqa: BLE001 - a retired generation must not kill the shell.
            logger.warning("旧应用 Runtime 退出异常：%s", exc)

    async def _ensure_started(self) -> None:
        gate, lock = self._primitives()
        self._loop = asyncio.get_running_loop()
        if self._current is not None:
            return
        async with lock:
            if self._current is not None:
                return
            self._current = await self._start_generation()
            gate.set()

    def _schedule_reload(self) -> bool:
        if self._shutting_down:
            return False
        gate, _ = self._primitives()
        gate.clear()
        if self._reload_task is None or self._reload_task.done():
            self._reload_task = asyncio.create_task(self._reload())
        return True

    async def request_reload(self) -> bool:
        """Schedule an in-process application generation swap."""
        return self._schedule_reload()

    def request_reload_threadsafe(self) -> bool:
        """Schedule a reload from either the ASGI loop or a background worker thread."""
        if self._shutting_down:
            return False
        loop = self._loop
        if loop is None or not loop.is_running():
            return False
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            return self._schedule_reload()
        loop.call_soon_threadsafe(self._schedule_reload)
        return True

    def runtime_status(self) -> dict[str, object]:
        task = self._reload_task
        return {
            "reloading": bool(task is not None and not task.done()),
            "last_error": self._last_reload_error,
        }

    async def _reload(self) -> None:
        gate, _ = self._primitives()
        old = self._current
        candidate: _Generation | None = None
        try:
            candidate = await self._start_generation()
        except Exception as exc:  # noqa: BLE001 - keep the currently running generation alive.
            self._last_reload_error = f"{type(exc).__name__}: {exc}"
            logger.exception("插件配置热重载失败")
            gate.set()
            return

        self._current = candidate
        self._last_reload_error = ""

        if old is not None:
            await old.drained.wait()
            await self._stop_generation(old)
        gate.set()

    async def _serve(self, scope, receive, send) -> None:
        await self._ensure_started()
        gate, _ = self._primitives()
        await gate.wait()
        generation = self._current
        if generation is None:
            raise RuntimeError("application generation is unavailable")

        generation.active_requests += 1
        generation.drained.clear()
        try:
            await generation.app(scope, receive, send)
        finally:
            generation.active_requests -= 1
            if generation.active_requests <= 0:
                generation.active_requests = 0
                generation.drained.set()

    async def _lifespan(self, receive, send) -> None:
        message = await receive()
        if message.get("type") != "lifespan.startup":
            return
        try:
            await self._ensure_started()
        except Exception as exc:  # noqa: BLE001
            await send({"type": "lifespan.startup.failed", "message": str(exc)})
            return
        await send({"type": "lifespan.startup.complete"})

        while True:
            message = await receive()
            if message.get("type") != "lifespan.shutdown":
                continue
            self._shutting_down = True
            gate, _ = self._primitives()
            gate.clear()
            task = self._reload_task
            if task is not None and not task.done():
                try:
                    await task
                except Exception as exc:  # noqa: BLE001
                    logger.warning("关闭前等待热重载失败：%s", exc)
            current = self._current
            if current is not None:
                await current.drained.wait()
                await self._stop_generation(current)
                self._current = None
            await send({"type": "lifespan.shutdown.complete"})
            return

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        await self._serve(scope, receive, send)


def create_reloadable_app(
    config: AppConfig | None = None,
    enabled_plugins: Iterable[str] | None = None,
) -> ReloadableApplication:
    app_config = config or AppConfig()
    enabled = None if enabled_plugins is None else tuple(enabled_plugins)
    shared_task_manager = TaskManager()

    def factory(reload_callback, reload_status) -> FastAPI:
        return create_app(
            app_config,
            enabled_plugins=enabled,
            reload_callback=reload_callback,
            reload_status=reload_status,
            shared_task_manager=shared_task_manager,
        )

    return ReloadableApplication(factory)


__all__ = ["ReloadableApplication", "create_reloadable_app"]
