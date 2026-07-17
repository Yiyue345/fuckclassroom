from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from fuckclassroom.config import AppConfig
from fuckclassroom.course_selection import (
    CourseSelectionApiError,
    CourseSelectionAssistant,
    SelectionBatch,
    _selection_batches_from_api,
)


class SelectionBatchTests(unittest.TestCase):
    def test_batch_parser_keeps_open_time_range(self) -> None:
        batches = _selection_batches_from_api(
            {
                "result": False,
                "data": [
                    {
                        "id": 1822,
                        "name": "小学期选课",
                        "allowEnter": True,
                        "openDateTimeRange": {
                            "startDateTime": "2026-05-12 09:50:00",
                            "endDateTime": "2026-07-19 23:00:00",
                        },
                    }
                ],
            },
            student_id=224039,
        )

        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].start_at, "2026-05-12 09:50")
        self.assertEqual(batches[0].end_at, "2026-07-19 23:00")
        self.assertTrue(batches[0].is_open)

    def test_selected_batch_is_fetched_without_falling_back(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = CourseSelectionAssistant(AppConfig(data_dir=Path(directory)))
            first = SelectionBatch(1, 10, "第一批次", "first")
            selected = SelectionBatch(1, 20, "第二批次", "selected")
            with (
                patch.object(assistant, "_selection_http_client", return_value=(object(), object())),
                patch.object(
                    assistant,
                    "_fetch_selection_batches",
                    return_value=([first, selected], "token"),
                ),
                patch.object(
                    assistant,
                    "_fetch_selection_batch_payload",
                    return_value={"turnId": 20, "studentId": 1},
                ) as fetch_payload,
                patch.object(assistant, "_persist_selection_cookies"),
            ):
                payload, batch = assistant._fetch_selection_info_via_api(
                    None,
                    10,
                    student_id=1,
                    turn_id=20,
                )

        self.assertEqual(batch, selected)
        self.assertEqual(payload["title"], "第二批次")
        self.assertEqual(payload["batches"], [asdict(first), asdict(selected)])
        fetch_payload.assert_called_once()

    def test_unavailable_selected_batch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = CourseSelectionAssistant(AppConfig(data_dir=Path(directory)))
            batch = SelectionBatch(1, 10, "第一批次", "first")
            with (
                patch.object(assistant, "_selection_http_client", return_value=(object(), object())),
                patch.object(
                    assistant,
                    "_fetch_selection_batches",
                    return_value=([batch], "token"),
                ),
                patch.object(assistant, "_fetch_selection_batch_payload") as fetch_payload,
            ):
                with self.assertRaisesRegex(CourseSelectionApiError, "所选批次当前不可用"):
                    assistant._fetch_selection_info_via_api(
                        None,
                        10,
                        student_id=1,
                        turn_id=99,
                    )

        fetch_payload.assert_not_called()

    def test_cached_info_exposes_current_batch(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = CourseSelectionAssistant(AppConfig(data_dir=Path(directory)))
            assistant.selection_info_path.parent.mkdir(parents=True, exist_ok=True)
            assistant.selection_info_path.write_text(
                json.dumps(
                    {
                        "title": "第二批次",
                        "turn_id": 20,
                        "student_id": 1,
                        "sections": {"major-plan": []},
                        "batches": [
                            {
                                "student_id": 1,
                                "turn_id": 20,
                                "label": "第二批次",
                                "is_open": True,
                                "start_at": "2026-05-12 09:50",
                                "end_at": "2026-07-19 23:00",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            info = assistant.get_selection_info()

        self.assertEqual(len(info.batches), 1)
        self.assertIsNotNone(info.current_batch)
        self.assertEqual(info.current_batch["label"], "第二批次")
        self.assertEqual(info.current_batch["start_at"], "2026-05-12 09:50")


    def test_refresh_forwards_selected_batch(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = CourseSelectionAssistant(AppConfig(data_dir=Path(directory)))
            batch = SelectionBatch(1, 20, "第二批次", "selected")
            with patch.object(
                assistant,
                "_fetch_selection_info_via_api",
                return_value=(
                    {"turnId": 20, "studentId": 1, "title": "第二批次", "batches": []},
                    batch,
                ),
            ) as fetch_info:
                result = assistant.refresh_selection_info_for_web(
                    None,
                    timeout_ms=10_000,
                    result_url="/selection/info",
                    student_id=1,
                    turn_id=20,
                )

        self.assertEqual(result, "/selection/info")
        fetch_info.assert_called_once_with(
            None,
            10.0,
            student_id=1,
            turn_id=20,
        )

if __name__ == "__main__":
    unittest.main()

