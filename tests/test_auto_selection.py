from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    def __init__(self, outcomes: list[Exception | None] | None = None) -> None:
        self.outcomes = list(outcomes or [None])
        self.calls: list[tuple[int, SelectionBatch, dict]] = []
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


    def test_capacity_full_stops_without_retry(self) -> None:
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

        self.assertEqual(job["status"], "full")
        self.assertIsNone(job["next_attempt_at"])
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


if __name__ == "__main__":
    unittest.main()

