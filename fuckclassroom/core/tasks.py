from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable


class TaskCancelledError(RuntimeError):
    """Raised cooperatively when a running background task is cancelled."""


@dataclass
class TaskLogEntry:
    timestamp: str
    level: str
    message: str


@dataclass
class TaskState:
    id: str
    title: str
    status: str
    progress: int
    message: str
    result_url: str | None
    error: str | None
    created_at: str
    updated_at: str
    auto_open_result: bool = False
    detail_progress: int | None = None
    detail_message: str | None = None
    cancel_requested: bool = False
    cancelled_at: str | None = None
    logs: list[TaskLogEntry] = field(default_factory=list)


_TASK_REGISTRY_LOCK = threading.Lock()
_TASK_OWNERS: dict[str, "TaskManager"] = {}
_MAX_LOG_ENTRIES = 800
_MAX_LOG_MESSAGE_LENGTH = 8000


class TaskProgressReporter:
    """Callable task reporter with detail progress, logs, and cooperative cancellation."""

    def __init__(self, manager: "TaskManager", task_id: str) -> None:
        self._manager = manager
        self._task_id = task_id

    def __call__(self, progress: int, message: str) -> None:
        self.raise_if_cancelled()
        self._manager._update(self._task_id, progress=progress, message=message)

    def detail(self, progress: int, message: str) -> None:
        self.raise_if_cancelled()
        self._manager._update(
            self._task_id,
            detail_progress=max(0, min(100, int(progress))),
            detail_message=message,
        )

    def clear_detail(self) -> None:
        self.raise_if_cancelled()
        self._manager._update(self._task_id, clear_detail=True)

    def log(self, message: str, level: str = "info") -> None:
        self.raise_if_cancelled()
        self._manager._append_log(self._task_id, level, message)

    @property
    def cancel_requested(self) -> bool:
        return self._manager.is_cancel_requested(self._task_id)

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested:
            raise TaskCancelledError("任务已取消")


class TaskManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskState] = {}

    def start(
        self,
        title: str,
        worker: Callable[[Callable[[int, str], None]], str | None],
        *,
        auto_open_result: bool = False,
    ) -> TaskState:
        task_id = uuid.uuid4().hex
        now = _now()
        state = TaskState(
            id=task_id,
            title=title,
            status="running",
            progress=0,
            message="准备开始",
            result_url=None,
            error=None,
            created_at=now,
            updated_at=now,
            auto_open_result=bool(auto_open_result),
            logs=[TaskLogEntry(now, "info", "任务已创建")],
        )
        with self._lock:
            self._tasks[task_id] = state
        with _TASK_REGISTRY_LOCK:
            _TASK_OWNERS[task_id] = self

        thread = threading.Thread(target=self._run, args=(task_id, worker), daemon=True)
        thread.start()
        return state

    def get(self, task_id: str) -> TaskState | None:
        with self._lock:
            return self._tasks.get(task_id)

    def as_dict(self, task_id: str) -> dict[str, object] | None:
        with self._lock:
            state = self._tasks.get(task_id)
            return asdict(state) if state else None

    def list_recent(self, limit: int = 20) -> list[TaskState]:
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda state: state.created_at, reverse=True)
            return tasks[: max(0, limit)]

    def is_cancel_requested(self, task_id: str) -> bool:
        with self._lock:
            state = self._tasks.get(task_id)
            return bool(state and state.cancel_requested)

    def cancel(self, task_id: str) -> dict[str, object]:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                raise KeyError(task_id)
            if state.status != "running":
                return asdict(state)
            if not state.cancel_requested:
                state.cancel_requested = True
                state.message = "正在取消任务"
                state.updated_at = _now()
                self._append_log_locked(state, "warning", "收到取消请求，等待当前操作到达安全取消点")
            return asdict(state)

    def _run(
        self,
        task_id: str,
        worker: Callable[[Callable[[int, str], None]], str | None],
    ) -> None:
        reporter = TaskProgressReporter(self, task_id)
        self._append_log(task_id, "info", "后台 Worker 已启动")

        try:
            reporter.raise_if_cancelled()
            result_url = worker(reporter)
            reporter.raise_if_cancelled()
        except TaskCancelledError:
            self._mark_cancelled(task_id)
            return
        except Exception as exc:  # noqa: BLE001 - task pages should show any worker failure.
            if self.is_cancel_requested(task_id):
                self._append_log(task_id, "debug", f"取消过程中 Worker 退出：{exc}")
                self._mark_cancelled(task_id)
                return
            stack = traceback.format_exc(limit=30).strip()
            self._append_log(task_id, "error", f"{type(exc).__name__}: {exc}")
            if stack:
                self._append_log(task_id, "debug", stack)
            self._update(
                task_id,
                status="failed",
                progress=100,
                message="任务失败",
                error=str(exc),
                clear_detail=True,
            )
            return

        self._append_log(task_id, "info", "任务执行完成")
        self._update(
            task_id,
            status="succeeded",
            progress=100,
            message="任务完成",
            result_url=result_url,
            clear_detail=True,
        )

    def _mark_cancelled(self, task_id: str) -> None:
        self._append_log(task_id, "warning", "任务已在安全取消点停止")
        self._update(
            task_id,
            status="cancelled",
            message="任务已取消",
            clear_detail=True,
            cancelled_at=_now(),
        )

    def _append_log(self, task_id: str, level: str, message: str) -> None:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                return
            self._append_log_locked(state, level, message)
            state.updated_at = _now()

    @staticmethod
    def _append_log_locked(state: TaskState, level: str, message: str) -> None:
        normalized_level = level if level in {"debug", "info", "warning", "error"} else "info"
        normalized_message = str(message or "").strip()
        if not normalized_message:
            return
        if len(normalized_message) > _MAX_LOG_MESSAGE_LENGTH:
            normalized_message = normalized_message[:_MAX_LOG_MESSAGE_LENGTH] + "\n…（日志已截断）"
        if state.logs and state.logs[-1].level == normalized_level and state.logs[-1].message == normalized_message:
            return
        state.logs.append(TaskLogEntry(_now(), normalized_level, normalized_message))
        if len(state.logs) > _MAX_LOG_ENTRIES:
            del state.logs[: len(state.logs) - _MAX_LOG_ENTRIES]

    def _update(
        self,
        task_id: str,
        *,
        status: str | None = None,
        progress: int | None = None,
        message: str | None = None,
        result_url: str | None = None,
        error: str | None = None,
        detail_progress: int | None = None,
        detail_message: str | None = None,
        clear_detail: bool = False,
        cancelled_at: str | None = None,
    ) -> None:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                return
            previous_message = state.message
            previous_detail_message = state.detail_message
            if status is not None:
                state.status = status
            if progress is not None:
                state.progress = max(0, min(100, int(progress)))
            if message is not None:
                state.message = message
            if result_url is not None:
                state.result_url = result_url
            if error is not None:
                state.error = error
            if cancelled_at is not None:
                state.cancelled_at = cancelled_at
            if clear_detail:
                state.detail_progress = None
                state.detail_message = None
            else:
                if detail_progress is not None:
                    state.detail_progress = max(0, min(100, int(detail_progress)))
                if detail_message is not None:
                    state.detail_message = detail_message

            if message is not None and message != previous_message:
                self._append_log_locked(state, "info", message)
            if detail_message is not None and detail_message != previous_detail_message:
                self._append_log_locked(state, "debug", detail_message)
            if error is not None:
                self._append_log_locked(state, "error", error)
            state.updated_at = _now()


def cancel_registered_task(task_id: str) -> dict[str, object]:
    """Cancel a task without requiring access to the owning TaskManager instance."""
    with _TASK_REGISTRY_LOCK:
        owner = _TASK_OWNERS.get(task_id)
    if owner is None:
        raise KeyError(task_id)
    return owner.cancel(task_id)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
