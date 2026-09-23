from __future__ import annotations

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path

import fuckclassroom.plugins.course_selection.services as selection_services
from fuckclassroom.plugins.course_selection.services import (
    AutoSelectionProcessProxy,
    CourseSelectionProcessFacade,
)


class _LocalSelection:
    def __init__(self, root: Path) -> None:
        self.session_dir = root / "webvpn"

    def defer_hy2_start(self):
        return nullcontext()

    def invalidate_access_mode(self) -> None:
        pass

    def clear_session(self) -> None:
        pass


class _CatalogHost:
    def __init__(self, *, query_result=None) -> None:
        self.query_result = query_result
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call_sync(self, method, params=None, *, progress=None, timeout=60):
        payload = dict(params or {})
        self.calls.append((method, payload))
        if method == "selection.catalog.query":
            return self.query_result
        if method == "selection.catalog.action":
            return "/selection/catalog"
        if method == "selection.catalog.remember":
            return {"ok": True}
        if method == "selection.catalog.cached":
            raise AssertionError("persisted cache should be used before worker memory")
        return {"ok": True}



class _AutoHost:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def call_sync(self, method, params=None, *, progress=None, timeout=60):
        self.calls.append(method)
        return {"ok": True}


class StatefulWorkerFacadeTests(unittest.TestCase):

    def test_auto_selection_execution_is_handed_off_without_overlap(self) -> None:
        old_host = _AutoHost()
        new_host = _AutoHost()
        old = AutoSelectionProcessProxy(old_host)  # type: ignore[arg-type]
        new = AutoSelectionProcessProxy(new_host)  # type: ignore[arg-type]
        selection_services._AUTO_SELECTION_OWNER = None
        selection_services._AUTO_SELECTION_PENDING = None
        try:
            old.start()
            new.start()

            self.assertEqual(old_host.calls, ["selection.auto.start"])
            self.assertEqual(new_host.calls, [])

            old.stop()

            self.assertEqual(
                old_host.calls,
                ["selection.auto.start", "selection.auto.stop"],
            )
            self.assertEqual(new_host.calls, ["selection.auto.start"])

            new.stop()
            self.assertEqual(
                new_host.calls,
                ["selection.auto.start", "selection.auto.stop"],
            )
        finally:
            selection_services._AUTO_SELECTION_OWNER = None
            selection_services._AUTO_SELECTION_PENDING = None

    def test_catalog_cache_survives_worker_facade_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_host = _CatalogHost(
                query_result={
                    "courses": [
                        {
                            "lesson_id": 123,
                            "lesson_name": "操作系统",
                            "course_name": "操作系统",
                        }
                    ],
                    "pagination": {"page": 1, "page_size": 50, "total": 1, "pages": 1},
                }
            )
            first = CourseSelectionProcessFacade(_LocalSelection(root), first_host)  # type: ignore[arg-type]

            result = first.query_whole_school_courses({}, page=1, page_size=50)

            self.assertEqual(result["courses"][0]["lesson_id"], 123)
            cache_path = root / "webvpn" / "catalog_worker_cache.json"
            self.assertTrue(cache_path.exists())

            second_host = _CatalogHost()
            second = CourseSelectionProcessFacade(_LocalSelection(root), second_host)  # type: ignore[arg-type]

            cached = second.get_cached_catalog_course(123)
            result_url = second.run_catalog_lesson_action_for_web(123, "select")

            self.assertEqual(cached["lesson_name"], "操作系统")
            self.assertEqual(result_url, "/selection/catalog")
            self.assertEqual(
                [method for method, _ in second_host.calls],
                ["selection.catalog.remember", "selection.catalog.action"],
            )
            self.assertEqual(
                second_host.calls[0][1]["course"]["lesson_id"],
                123,
            )


if __name__ == "__main__":
    unittest.main()
