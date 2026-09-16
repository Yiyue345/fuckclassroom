from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from fuckclassroom.classroom import ClassroomClient, ClassroomClientError, Term
from fuckclassroom.classroom.semester_sync import SemesterSnapshot, SemesterSyncService
from fuckclassroom.core.config import AppConfig
from fuckclassroom.web import create_app


def _term(term_id: str, *, current: bool = False) -> Term:
    return Term(
        id=term_id,
        name=f"学期 {term_id}",
        year="2026-2027",
        season=term_id,
        begin_date="2026-09-01",
        end_date="2027-01-31",
        current=current,
    )


class _FakeClassroomClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls = 0

    def refresh_terms(self) -> list[Term]:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return list(response)


class SemesterSyncTests(unittest.TestCase):
    def test_sync_success_caches_terms_and_current_semester(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            client = _FakeClassroomClient([[_term("1"), _term("2", current=True)]])
            service = SemesterSyncService(
                client,  # type: ignore[arg-type]
                Path(directory) / "semesters.json",
            )

            snapshot = service.sync(force=True)

            self.assertFalse(snapshot.from_cache)
            self.assertEqual(snapshot.current_term_id, "2")
            self.assertTrue(service.cache_path.exists())

    def test_sync_failure_uses_cached_semesters(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            client = _FakeClassroomClient(
                [[_term("2", current=True)], ClassroomClientError("网络不可用")]
            )
            service = SemesterSyncService(
                client,  # type: ignore[arg-type]
                Path(directory) / "semesters.json",
            )
            service.sync(force=True)

            snapshot = service.sync(force=True)

            self.assertTrue(snapshot.from_cache)
            self.assertEqual(snapshot.current_term_id, "2")
            self.assertEqual(snapshot.error, "网络不可用")

    def test_expired_cache_is_refreshed(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            current_time = [datetime(2026, 9, 1, tzinfo=timezone.utc)]
            client = _FakeClassroomClient([[_term("1", current=True)], [_term("2", current=True)]])
            service = SemesterSyncService(
                client,  # type: ignore[arg-type]
                Path(directory) / "semesters.json",
                ttl_seconds=60,
                now=lambda: current_time[0],
            )

            first = service.sync()
            current_time[0] += timedelta(seconds=30)
            fresh_cache = service.sync()
            current_time[0] += timedelta(seconds=31)
            refreshed = service.sync()

            self.assertEqual(first.current_term_id, "1")
            self.assertTrue(fresh_cache.from_cache)
            self.assertEqual(refreshed.current_term_id, "2")
            self.assertEqual(client.calls, 2)

    def test_courses_page_defaults_to_current_semester(self) -> None:
        current = _term("current", current=True)
        snapshot = SemesterSnapshot(
            terms=(_term("old"), current),
            current_term_id=current.id,
            updated_at=datetime.now(timezone.utc),
        )
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, patch.object(
            SemesterSyncService,
            "sync",
            return_value=snapshot,
        ), patch.object(ClassroomClient, "list_courses", return_value=[]) as list_courses:
            app = create_app(AppConfig(data_dir=Path(directory)))
            response = TestClient(app).get("/courses")

        self.assertEqual(response.status_code, 200)
        self.assertIn('value="current" selected', response.text)
        self.assertEqual(list_courses.call_args.kwargs["term"], current)
        self.assertTrue(list_courses.call_args.kwargs["force_refresh"])

    def test_force_refresh_courses_bypasses_cached_course_list(self) -> None:
        payload = {
            "params": {
                "result": {
                    "data": [
                        {
                            "Id": "fresh-course",
                            "Title": "最新课程",
                            "StartAt": 1788192000,
                            "EndAt": 1798732800,
                        }
                    ]
                }
            }
        }
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            client = ClassroomClient(AppConfig(data_dir=Path(directory)))
            with patch.object(client, "_get_json", return_value=payload) as remote, patch.object(
                client,
                "_get_json_cached",
            ) as cached, patch.object(client.cache, "set") as cache_set:
                courses = client.list_courses(force_refresh=True)

        self.assertEqual([course.id for course in courses], ["fresh-course"])
        remote.assert_called_once()
        cached.assert_not_called()
        cache_set.assert_called_once_with("courses_page_1_size_50", payload)


if __name__ == "__main__":
    unittest.main()
