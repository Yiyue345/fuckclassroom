from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fuckclassroom.config import AppConfig
from fuckclassroom.classroom.client import (
    Course,
    CourseDetail,
    ClassroomClient,
    Lesson,
    LessonResource,
)


_PPT_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class PptExportCacheRefreshTests(unittest.TestCase):
    def _detail(self, status: str) -> CourseDetail:
        course = Course(
            id="course-1",
            title="测试课程",
            teacher="",
            type="",
            model="",
            start_at="",
            end_at="",
        )
        lesson = Lesson(
            id="lesson-1",
            title="第一讲",
            status_label="直播中" if status == "running" else "",
            start_at="",
            end_at="",
            duration_seconds=None,
            lecturer="",
            resources=[
                LessonResource(
                    kind="ppt",
                    title="下载课件",
                    url="https://classroom.guet.edu.cn/ppt",
                    status=status,
                )
            ],
            is_live=status == "running",
        )
        return CourseDetail(course=course, lessons=[lesson])

    @staticmethod
    def _ppt_path(config: AppConfig) -> Path:
        return (
            config.downloads_dir
            / "测试课程"
            / "第一讲"
            / "测试课程_第一讲_课件.pptx"
        )

    def test_legacy_live_ppt_is_redownloaded_once_recording_is_final(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = AppConfig(data_dir=Path(directory))
            client = ClassroomClient(config)
            ppt_path = self._ppt_path(config)
            ppt_path.parent.mkdir(parents=True, exist_ok=True)
            ppt_path.write_bytes(b"partial-live-ppt")

            with (
                patch.object(client, "get_course_detail", return_value=self._detail("running")),
                patch.object(client, "refresh_course_detail", return_value=self._detail("finished")),
                patch.object(client, "_get_bytes", return_value=(b"complete-recording-ppt", _PPT_MIME)) as download,
            ):
                exported = client.download_lesson_export("course-1", "lesson-1", "ppt")

            self.assertFalse(exported.from_cache)
            self.assertEqual(ppt_path.read_bytes(), b"complete-recording-ppt")
            self.assertTrue(download.called)
            metadata = json.loads(
                ppt_path.with_suffix(".pptx.meta.json").read_text(encoding="utf-8")
            )
            self.assertFalse(metadata["provisional"])
            self.assertEqual(metadata["ppt_status"], "finished")

    def test_live_ppt_cache_stays_provisional_and_is_reused_until_live_ends(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = AppConfig(data_dir=Path(directory))
            client = ClassroomClient(config)
            ppt_path = self._ppt_path(config)
            ppt_path.parent.mkdir(parents=True, exist_ok=True)
            ppt_path.write_bytes(b"partial-live-ppt")

            with (
                patch.object(client, "get_course_detail", return_value=self._detail("running")),
                patch.object(client, "refresh_course_detail", return_value=self._detail("running")),
                patch.object(client, "_get_bytes", side_effect=AssertionError("live cache should be reused")),
            ):
                exported = client.download_lesson_export("course-1", "lesson-1", "ppt")

            self.assertTrue(exported.from_cache)
            self.assertEqual(exported.content, b"partial-live-ppt")
            metadata = json.loads(
                ppt_path.with_suffix(".pptx.meta.json").read_text(encoding="utf-8")
            )
            self.assertTrue(metadata["provisional"])
            self.assertEqual(metadata["ppt_status"], "running")


if __name__ == "__main__":
    unittest.main()
