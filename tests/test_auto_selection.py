from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fuckclassroom.course_selection.automation import AutoSelectionService
from fuckclassroom.config import AppConfig
from fuckclassroom.course_selection import (
    CourseSelectionApiError,
    CourseSelectionAssistant,
    SelectionBatch,
)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class FakeAssistant:
    def __init__(
        self,
        outcomes: list[Exception | None] | None = None,
        capacity_results: list[dict | Exception] | None = None,
        session_results: list[SimpleNamespace | Exception] | None = None,
    ) -> None:
        self.outcomes = list(outcomes or [None])
        self.capacity_results = list(capacity_results or [])
        self.session_results = list(session_results or [])
        self.verify_calls = 0
        self.calls: list[tuple[int, SelectionBatch, dict]] = []
        self.capacity_calls: list[tuple[int, SelectionBatch, dict]] = []
        self.called = threading.Event()
        self.called_at: float | None = None

    def select_lesson_for_batch_once(
        self,
        lesson_id: int,
        batch: SelectionBatch,
        course: dict,
        timeout_ms: int = 60_000,
    ) -> str:
        self.calls.append((lesson_id, batch, course))
        self.called_at = time.monotonic()
        self.called.set()
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        return "/selection/auto"

    def get_lesson_capacity_for_batch(
        self,
        lesson_id: int,
        batch: SelectionBatch,
        course: dict,
        timeout_ms: int = 60_000,
    ) -> dict:
        self.capacity_calls.append((lesson_id, batch, course))
        if self.capacity_results:
            result = self.capacity_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return {
            "selected_count": course.get("selected_count"),
            "max_count": course.get("max_count"),
        }

    def verify_saved_session(
        self,
        timeout_ms: int = 15_000,
        *,
        auto_relogin: bool = True,
    ):
        self.verify_calls += 1
        if self.session_results:
            result = self.session_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return SimpleNamespace(is_valid=False, message="未保存可用的自动登录会话")


class PendingBatchAssistant(FakeAssistant):
    def __init__(self, batch: dict | Exception) -> None:
        super().__init__()
        self.batch = batch
        self.batch_calls = 0

    def get_available_catalog_batch(self, timeout_ms: int = 60_000) -> dict:
        self.batch_calls += 1
        if isinstance(self.batch, Exception):
            raise self.batch
        return self.batch


class AutoSelectionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.zone = timezone(timedelta(hours=8))
        self.clock = MutableClock(datetime(2026, 7, 17, 9, 0, tzinfo=self.zone))
        self.course = {
            "lesson_id": 88,
            "course_name": "数据结构",
            "lesson_name": "数据结构-01",
            "lesson_code": "DS01",
            "teachers": "张老师",
            "time": "周一 1-2 节",
            "max_count": 60,
            "selected_count": 20,
            "is_selected": False,
        }

    def batch(self, start: datetime, end: datetime) -> dict:
        return {
            "student_id": 1,
            "turn_id": 2,
            "label": "第一轮选课",
            "url": "https://example.invalid/select",
            "is_open": False,
            "start_at": start.strftime("%Y-%m-%d %H:%M"),
            "end_at": end.strftime("%Y-%m-%d %H:%M"),
        }

    def service(self, directory: str, assistant: FakeAssistant) -> AutoSelectionService:
        return AutoSelectionService(
            assistant,
            Path(directory) / "jobs.json",
            now_provider=self.clock,
        )

    def test_waits_until_batch_start_then_selects(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = FakeAssistant()
            service = self.service(directory, assistant)
            start = self.clock.value + timedelta(minutes=10)
            service.add_job(88, self.course, self.batch(start, start + timedelta(hours=2)))

            self.assertFalse(service.run_due_once())
            self.assertEqual(assistant.calls, [])

            self.clock.advance(600)
            self.assertTrue(service.run_due_once())
            job = service.list_jobs()[0]

        self.assertEqual(job["status"], "selected")
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(assistant.calls[0][1].turn_id, 2)

    def test_background_worker_wakes_close_to_start_time(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = FakeAssistant()
            service = AutoSelectionService(
                assistant,
                Path(directory) / "jobs.json",
            )
            now = datetime.now().astimezone()
            start = (now + timedelta(seconds=2)).replace(microsecond=0)
            batch = {
                "student_id": 1,
                "turn_id": 2,
                "label": "第一轮选课",
                "url": "https://example.invalid/select",
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(seconds=10)).isoformat(),
            }
            service.add_job(88, self.course, batch)
            started_at = time.monotonic()
            service.start()
            try:
                early = assistant.called.wait(0.15)
                called = assistant.called.wait(2)
            finally:
                service.stop()
            elapsed = (assistant.called_at or time.monotonic()) - started_at
            expected_delay = (start - now).total_seconds()
            status = service.list_jobs()[0]["status"]

        self.assertFalse(early)
        self.assertTrue(called)
        self.assertGreaterEqual(elapsed, expected_delay - 0.15)
        self.assertLess(elapsed, expected_delay + 0.5)
        self.assertEqual(status, "selected")

    def test_capacity_full_switches_to_vacancy_monitoring(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = FakeAssistant([RuntimeError("课程容量已满（60/60）")])
            service = self.service(directory, assistant)
            service.add_job(
                88,
                self.course,
                self.batch(self.clock.value - timedelta(minutes=1), self.clock.value + timedelta(hours=1)),
            )

            service.run_due_once()
            job = service.list_jobs()[0]

        self.assertEqual(job["status"], "waiting_capacity")
        self.assertIsNotNone(job["next_attempt_at"])
        self.assertTrue(job["monitor_capacity"])
        self.assertEqual(len(assistant.calls), 1)

    def test_full_course_polls_capacity_and_selects_after_a_seat_opens(self) -> None:
        full_course = dict(self.course, selected_count=60)
        capacity_results = [
            {"selected_count": 60, "max_count": 60},
            {"selected_count": 59, "max_count": 60},
        ]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = FakeAssistant(capacity_results=capacity_results)
            service = self.service(directory, assistant)
            service.add_job(
                88,
                full_course,
                self.batch(self.clock.value - timedelta(minutes=1), self.clock.value + timedelta(hours=1)),
            )

            self.assertEqual(service.list_jobs()[0]["status"], "waiting_capacity")
            service.run_due_once()
            waiting = service.list_jobs()[0]
            self.assertEqual(waiting["status"], "waiting_capacity")
            self.assertEqual(waiting["capacity_checks"], 1)
            self.assertEqual(assistant.calls, [])

            self.clock.advance(30)
            service.run_due_once()
            selected = service.list_jobs()[0]

        self.assertEqual(selected["status"], "selected")
        self.assertEqual(selected["capacity_checks"], 2)
        self.assertEqual(selected["attempts"], 1)
        self.assertEqual(selected["last_selected_count"], 59)
        self.assertEqual(len(assistant.calls), 1)

    def test_invalid_session_waits_then_retries(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = FakeAssistant([RuntimeError("教务会话失效，请重新登录"), None])
            service = self.service(directory, assistant)
            service.add_job(
                88,
                self.course,
                self.batch(self.clock.value - timedelta(minutes=1), self.clock.value + timedelta(hours=1)),
            )

            service.run_due_once()
            self.assertEqual(service.list_jobs()[0]["status"], "waiting_session")
            self.assertFalse(service.run_due_once())

            self.clock.advance(30)
            service.run_due_once()
            job = service.list_jobs()[0]

        self.assertEqual(job["status"], "selected")
        self.assertEqual(job["attempts"], 2)

    def test_invalid_session_automatically_relogs_in_with_saved_credentials(self) -> None:
        restored = SimpleNamespace(is_valid=True, message="教务会话有效")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = FakeAssistant(
                [RuntimeError("教务会话失效，请重新登录"), None],
                session_results=[restored],
            )
            service = self.service(directory, assistant)
            service.add_job(
                88,
                self.course,
                self.batch(
                    self.clock.value - timedelta(minutes=1),
                    self.clock.value + timedelta(hours=1),
                ),
            )

            service.run_due_once()
            waiting = service.list_jobs()[0]
            self.assertEqual(waiting["status"], "retrying")
            self.assertIn("自动恢复", waiting["message"])
            self.assertEqual(assistant.verify_calls, 1)

            self.clock.advance(5)
            service.run_due_once()
            selected = service.list_jobs()[0]

        self.assertEqual(selected["status"], "selected")

    def test_capacity_monitor_keeps_waiting_after_automatic_relogin(self) -> None:
        full_course = dict(self.course, selected_count=60)
        restored = SimpleNamespace(is_valid=True, message="教务会话有效")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = FakeAssistant(
                capacity_results=[
                    RuntimeError("教务会话失效，请重新登录"),
                    {"selected_count": 59, "max_count": 60},
                ],
                session_results=[restored],
            )
            service = self.service(directory, assistant)
            service.add_job(
                88,
                full_course,
                self.batch(
                    self.clock.value - timedelta(minutes=1),
                    self.clock.value + timedelta(hours=1),
                ),
            )

            service.run_due_once()
            waiting = service.list_jobs()[0]
            self.assertEqual(waiting["status"], "waiting_capacity")
            self.assertTrue(waiting["monitor_capacity"])

            self.clock.advance(5)
            service.run_due_once()
            selected = service.list_jobs()[0]

        self.assertEqual(selected["status"], "selected")
        self.assertEqual(assistant.verify_calls, 1)

    def test_retry_intervals_can_be_updated_at_runtime(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            service = self.service(directory, FakeAssistant())
            service.configure_intervals(
                retry_seconds=9,
                session_retry_seconds=45,
                capacity_retry_seconds=75,
            )

        self.assertEqual(service.retry_seconds, 9)
        self.assertEqual(service.session_retry_seconds, 45)
        self.assertEqual(service.capacity_retry_seconds, 75)

    def test_jobs_persist_and_can_be_removed_after_cancel(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = FakeAssistant()
            service = self.service(directory, assistant)
            job, _ = service.add_job(
                88,
                self.course,
                self.batch(self.clock.value + timedelta(minutes=30), self.clock.value + timedelta(hours=1)),
            )
            service.cancel(job.id)

            restored = self.service(directory, assistant)
            self.assertEqual(restored.list_jobs()[0]["status"], "cancelled")
            restored.delete(job.id)
            self.assertEqual(restored.list_jobs(), [])

    def test_legacy_full_job_resumes_as_capacity_monitor(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "jobs.json"
            path.write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "legacy-full",
                                "lesson_id": 88,
                                "student_id": 1,
                                "turn_id": 2,
                                "batch_label": "第一轮选课",
                                "batch_url": "https://example.invalid/select",
                                "start_at": (self.clock.value - timedelta(minutes=1)).isoformat(),
                                "end_at": (self.clock.value + timedelta(hours=1)).isoformat(),
                                "course": dict(self.course, selected_count=60),
                                "status": "full",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            restored = self.service(directory, FakeAssistant())
            job = restored.list_jobs()[0]

        self.assertEqual(job["status"], "waiting_capacity")
        self.assertTrue(job["monitor_capacity"])
        self.assertIsNotNone(job["next_attempt_at"])

    def test_batch_end_expires_without_request(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = FakeAssistant()
            service = self.service(directory, assistant)
            job, _ = service.add_job(
                88,
                self.course,
                self.batch(self.clock.value + timedelta(minutes=1), self.clock.value + timedelta(minutes=2)),
            )
            self.clock.advance(121)

            self.assertFalse(service.run_due_once())
            self.assertEqual(service.list_jobs()[0]["status"], "expired")
            self.assertEqual(job.attempts, 0)
            self.assertEqual(assistant.calls, [])


    def test_pending_catalog_job_binds_batch_before_selecting(self) -> None:
        start = self.clock.value - timedelta(minutes=1)
        batch = self.batch(start, start + timedelta(hours=1))
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = PendingBatchAssistant(batch)
            service = self.service(directory, assistant)
            service.add_job(88, self.course, None)

            waiting = service.list_jobs()[0]
            self.assertEqual(waiting["status"], "waiting_batch")
            self.assertEqual(waiting["attempts"], 0)

            self.assertTrue(service.run_due_once())
            bound = service.list_jobs()[0]
            self.assertEqual(bound["status"], "scheduled")
            self.assertEqual(bound["turn_id"], 2)
            self.assertEqual(bound["attempts"], 0)

            self.assertTrue(service.run_due_once())
            selected = service.list_jobs()[0]

        self.assertEqual(selected["status"], "selected")
        self.assertEqual(selected["attempts"], 1)
        self.assertEqual(assistant.batch_calls, 1)

    def test_pending_catalog_job_keeps_waiting_without_batch(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = PendingBatchAssistant(RuntimeError("当前还没有正式选课批次"))
            service = self.service(directory, assistant)
            service.add_job(88, self.course, None)

            self.assertTrue(service.run_due_once())
            waiting = service.list_jobs()[0]

        self.assertEqual(waiting["status"], "waiting_batch")
        self.assertEqual(waiting["attempts"], 0)
        self.assertIn("稍后自动检查", waiting["message"])


class CourseSelectionCapacityTests(unittest.TestCase):
    def test_selection_stops_when_live_capacity_is_full(self) -> None:
        lesson = {
            "id": 88,
            "course": {"nameZh": "数据结构"},
            "teachers": [],
            "limitCount": 60,
        }
        batch = SelectionBatch(1, 2, "第一轮选课", "https://example.invalid/select")

        def response(_opener, _token, path, **_kwargs):
            if path.startswith("/selected-lessons/"):
                return {"data": []}
            if path.startswith("/query-condition/"):
                return {"data": {"semesterAssoc": 3}}
            if path.startswith("/query-lesson/"):
                return {"data": [lesson]}
            if path.startswith("/std-count?"):
                return {"data": {"88": 60}}
            raise AssertionError(path)

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = CourseSelectionAssistant(AppConfig(data_dir=Path(directory)))
            with patch.object(assistant, "_request_selection_json", side_effect=response):
                with self.assertRaisesRegex(CourseSelectionApiError, "课程容量已满"):
                    assistant._fetch_selection_action_lesson(
                        object(),
                        "token",
                        batch,
                        88,
                        "select",
                        10,
                        max_count_hint=60,
                    )

    def test_capacity_monitor_uses_only_std_count_endpoint(self) -> None:
        batch = SelectionBatch(1, 2, "第一轮选课", "https://example.invalid/select")
        course = {"lesson_id": 88, "max_count": 60}
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = CourseSelectionAssistant(AppConfig(data_dir=Path(directory)))
            with (
                patch.object(assistant, "_selection_http_client", return_value=(object(), object())),
                patch.object(assistant, "_fetch_selection_token", return_value="token"),
                patch.object(
                    assistant,
                    "_request_selection_json",
                    return_value={"data": {"88": 59}},
                ) as request,
                patch.object(assistant, "_persist_selection_cookies"),
            ):
                capacity = assistant.get_lesson_capacity_for_batch(88, batch, course)

        self.assertEqual(capacity, {"selected_count": 59, "max_count": 60})
        self.assertEqual(request.call_count, 1)
        self.assertTrue(request.call_args.args[2].startswith("/std-count?"))


if __name__ == "__main__":
    unittest.main()

