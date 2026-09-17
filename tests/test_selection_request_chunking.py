from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fuckclassroom.config import AppConfig
from fuckclassroom.course_selection import CourseSelectionAssistant


class SelectionRequestChunkingTests(unittest.TestCase):
    def test_oversized_std_count_request_is_split_and_merged(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = CourseSelectionAssistant(AppConfig(data_dir=Path(directory)))
            lesson_ids = [str(100000 + index) for index in range(400)]
            path = f"/std-count?lessonIds={','.join(lesson_ids)}"
            seen_paths: list[str] = []

            def fake_request(
                _assistant,
                _opener,
                _token,
                chunk_path,
                *,
                method="GET",
                body=None,
                timeout_seconds,
            ):
                del method, body, timeout_seconds
                seen_paths.append(chunk_path)
                self.assertLessEqual(len(chunk_path), 1400)
                ids = parse_qs(urlparse(chunk_path).query)["lessonIds"][0].split(",")
                return {"data": {lesson_id: int(lesson_id) % 17 for lesson_id in ids}}

            with patch(
                "fuckclassroom.course_selection.api.SelectionApiMixin._request_selection_json",
                autospec=True,
                side_effect=fake_request,
            ):
                payload = assistant._request_selection_json(
                    object(),
                    "token",
                    path,
                    timeout_seconds=10.0,
                )

        self.assertGreater(len(seen_paths), 1)
        self.assertEqual(len(payload["data"]), len(lesson_ids))
        self.assertEqual(payload["data"][lesson_ids[0]], int(lesson_ids[0]) % 17)
        self.assertEqual(payload["data"][lesson_ids[-1]], int(lesson_ids[-1]) % 17)


if __name__ == "__main__":
    unittest.main()
