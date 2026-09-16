from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable


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
    detail_progress: int | None = None
    detail_message: str | None = None


class TaskProgressReporter:
    """Callable task reporter with an optional secondary/detail progress channel."""

    def __init__(self, manager: "TaskManager", task_id: str) -> None:
        self._manager = manager
        self._task_id = task_id

    def __call__(self, progress: int, message: str) -> None:
        self._manager._update(self._task_id, progress=progress, message=message)

    def detail(self, progress: int, message: str) -> None:
        self._manager._update(
            self._task_id,
            detail_progress=max(0, min(100, int(progress))),
            detail_message=message,
        )

    def clear_detail(self) -> None:
        self._manager._update(self._task_id, clear_detail=True)


class TaskManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskState] = {}

    def start(
        self,
        title: str,
        worker: Callable[[Callable[[int, str], None]], str | None],
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
        )
        with self._lock:
            self._tasks[task_id] = state

        thread = threading.Thread(target=self._run, args=(task_id, worker), daemon=True)
        thread.start()
        return state

    def get(self, task_id: str) -> TaskState | None:
        with self._lock:
            return self._tasks.get(task_id)

    def as_dict(self, task_id: str) -> dict[str, object] | None:
        state = self.get(task_id)
        return asdict(state) if state else None

    def list_recent(self, limit: int = 20) -> list[TaskState]:
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda state: state.created_at, reverse=True)
            return tasks[: max(0, limit)]

    def _run(
        self,
        task_id: str,
        worker: Callable[[Callable[[int, str], None]], str | None],
    ) -> None:
        reporter = TaskProgressReporter(self, task_id)

        try:
            result_url = worker(reporter)
        except Exception as exc:  # noqa: BLE001 - task pages should show any worker failure.
            self._update(
                task_id,
                status="failed",
                progress=100,
                message="任务失败",
                error=str(exc),
                clear_detail=True,
            )
            return
        self._update(
            task_id,
            status="succeeded",
            progress=100,
            message="任务完成",
            result_url=result_url,
            clear_detail=True,
        )

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
    ) -> None:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                return
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
            if clear_detail:
                state.detail_progress = None
                state.detail_message = None
            else:
                if detail_progress is not None:
                    state.detail_progress = max(0, min(100, int(detail_progress)))
                if detail_message is not None:
                    state.detail_message = detail_message
            state.updated_at = _now()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
