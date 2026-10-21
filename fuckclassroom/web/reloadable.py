from __future__ import annotations

import asyncio
import logging
import re
import shutil
import uuid
from pathlib import Path
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
        *,
        plugin_dir: Path | None = None,
    ) -> None:
        self._app_factory = app_factory
        self._current: _Generation | None = None
        self._request_gate: asyncio.Event | None = None
        self._reload_task: asyncio.Task[None] | None = None
        self._startup_lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_reload_error = ""
        self._shutting_down = False
        self._plugin_dir = Path(plugin_dir) if plugin_dir is not None else None
        self._update_lock: asyncio.Lock | None = None
        self._updating_plugin = False

    def _primitives(self) -> tuple[asyncio.Event, asyncio.Lock]:
        if self._request_gate is None:
            self._request_gate = asyncio.Event()
        if self._startup_lock is None:
            self._startup_lock = asyncio.Lock()
        if self._update_lock is None:
            self._update_lock = asyncio.Lock()
        return self._request_gate, self._startup_lock

    async def _start_generation(self) -> _Generation:
        app = self._app_factory(self.request_reload, self.runtime_status)
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
        if self._shutting_down or self._updating_plugin:
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

    async def _reload_and_wait_for_update(self) -> bool:
        """Await a fresh generation using the newly updated plugin files."""
        existing = self._reload_task
        if existing is not None and not existing.done():
            await existing
        if not self._schedule_reload():
            return False
        task = self._reload_task
        if task is None:
            return False
        await task
        if self._last_reload_error:
            raise RuntimeError(f"Runtime 热重载失败：{self._last_reload_error}")
        return True

    def reload_updated_plugin_from_worker(self) -> bool:
        """Wait for reload in a task worker without blocking the ASGI loop."""
        loop = self._loop
        if self._shutting_down or loop is None or not loop.is_running():
            return False
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError("不能在 ASGI 事件循环中同步等待 Runtime 热重载")
        return asyncio.run_coroutine_threadsafe(
            self._reload_and_wait_for_update(), loop
        ).result(timeout=120)

    @staticmethod
    def _restore_plugin_backup(
        destination: Path, backup: Path,
    ) -> None:
        """Restore the original directory without discarding the backup first."""
        displaced = destination.parent / f".{destination.name}.failed-{uuid.uuid4().hex}"
        if destination.exists():
            destination.replace(displaced)
        try:
            backup.replace(destination)
        except OSError:
            if displaced.exists() and not destination.exists():
                displaced.replace(destination)
            raise
        if displaced.exists():
            shutil.rmtree(displaced, ignore_errors=True)

    async def _apply_plugin_update(self, plugin_id: str, installer: Callable[[], Any]) -> Any:
        """Drain -> stop old Runtime -> replace -> start new, or roll back."""
        if self._plugin_dir is None:
            raise RuntimeError("当前启动方式不支持运行中插件自动更新")
        if re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", plugin_id) is None:
            raise ValueError("无效的插件 ID")
        self._primitives()
        assert self._update_lock is not None
        async with self._update_lock:
            if self._shutting_down:
                raise RuntimeError("应用正在关闭，无法更新插件")
            ongoing = self._reload_task
            if ongoing is not None and not ongoing.done():
                await ongoing
            gate, _ = self._primitives()
            old = self._current
            if old is None:
                raise RuntimeError("应用 Runtime 尚未启动")

            destination = self._plugin_dir / plugin_id
            if not destination.is_dir() or destination.is_symlink():
                raise RuntimeError("待更新插件目录不存在或不安全")
            backup = self._plugin_dir / f".{plugin_id}.rollback-{uuid.uuid4().hex}"
            backup_ready = False
            stopped = False
            self._updating_plugin = True
            gate.clear()
            try:
                # No new requests enter old routes while the update runs.
                # A long-lived request causes a safe failure, never a forced
                # replacement while code remains active.
                await asyncio.wait_for(old.drained.wait(), timeout=60)
                await self._stop_generation(old)
                self._current = None
                stopped = True

                await asyncio.to_thread(
                    shutil.copytree, destination, backup, symlinks=True,
                )
                backup_ready = True
                descriptor = await asyncio.to_thread(installer)
                candidate = await self._start_generation()
                self._current = candidate
                self._last_reload_error = ""
                return descriptor
            except BaseException as exc:
                if stopped:
                    rollback_error = None
                    if backup_ready:
                        try:
                            await asyncio.to_thread(
                                self._restore_plugin_backup, destination, backup,
                            )
                        except Exception as restore_exc:
                            rollback_error = restore_exc
                    if rollback_error is None:
                        try:
                            self._current = await self._start_generation()
                        except Exception as restart_exc:
                            rollback_error = restart_exc
                    if rollback_error is not None:
                        self._last_reload_error = (
                            f"更新失败：{exc}；回滚/恢复 Runtime 失败：{rollback_error}"
                        )
                        logger.exception("插件 %s 更新后自动恢复失败", plugin_id)
                        raise RuntimeError(self._last_reload_error) from exc
                    self._last_reload_error = f"插件更新失败，旧版本已恢复：{exc}"
                    raise RuntimeError(self._last_reload_error) from exc
                # Timed out waiting for inflight requests. The old
                # generation remains running and no plugin file was touched.
                raise
            finally:
                self._updating_plugin = False
                gate.set()
                if backup.exists() and self._current is not None:
                    await asyncio.to_thread(shutil.rmtree, backup, ignore_errors=True)

    def update_plugin_from_worker(
        self, plugin_id: str, installer: Callable[[], Any],
    ) -> Any:
        """Entry for a downloader task thread; it owns the entire update."""
        loop = self._loop
        if self._shutting_down or loop is None or not loop.is_running():
            raise RuntimeError("Runtime 不可用，无法安全更新运行中的插件")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError("不能在 ASGI 事件循环中同步更新插件")
        return asyncio.run_coroutine_threadsafe(
            self._apply_plugin_update(plugin_id, installer), loop,
        ).result()

    def runtime_status(self) -> dict[str, object]:
        task = self._reload_task
        return {
            "reloading": bool(task is not None and not task.done()) or self._updating_plugin,
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

    shell: ReloadableApplication

    def factory(reload_callback, reload_status) -> FastAPI:
        return create_app(
            app_config,
            enabled_plugins=enabled,
            reload_callback=reload_callback,
            reload_background_callback=shell.request_reload_threadsafe,
            reload_wait_callback=shell.reload_updated_plugin_from_worker,
            plugin_update_callback=shell.update_plugin_from_worker,
            reload_status=reload_status,
            shared_task_manager=shared_task_manager,
        )

    shell = ReloadableApplication(
        factory, plugin_dir=app_config.data_dir / "plugins",
    )
    return shell


__all__ = ["ReloadableApplication", "create_reloadable_app"]
