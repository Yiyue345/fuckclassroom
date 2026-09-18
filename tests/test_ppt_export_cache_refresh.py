from __future__ import annotations

import json
import io
import tempfile
import unittest
import zipfile
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
    def _detail(self, status: str, *, with_recording: bool = False) -> CourseDetail:
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
                ),
                *(
                    [
                        LessonResource(
                            kind="video",
                            title="课堂录像",
                            url="https://classroom.guet.edu.cn/recording.mp4",
                        )
                    ]
                    if with_recording
                    else []
                ),
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

    @staticmethod
    def _pptx_bytes(slide_count: int) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for index in range(1, slide_count + 1):
                archive.writestr(f"ppt/slides/slide{index}.xml", "<slide />")
        return output.getvalue()

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

    def test_recording_overrides_stale_running_ppt_status(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = AppConfig(data_dir=Path(directory))
            client = ClassroomClient(config)
            ppt_path = self._ppt_path(config)
            ppt_path.parent.mkdir(parents=True, exist_ok=True)
            ppt_path.write_bytes(b"partial-live-ppt")

            stale_detail = self._detail("running", with_recording=True)
            with (
                patch.object(client, "get_course_detail", return_value=stale_detail),
                patch.object(client, "refresh_course_detail", return_value=stale_detail),
                patch.object(client, "list_current_live_lessons", return_value=[]),
                patch.object(
                    client,
                    "_get_bytes",
                    return_value=(b"complete-recording-ppt", _PPT_MIME),
                ) as download,
            ):
                exported = client.download_lesson_export("course-1", "lesson-1", "ppt")

            self.assertFalse(exported.from_cache)
            self.assertEqual(ppt_path.read_bytes(), b"complete-recording-ppt")
            self.assertTrue(download.called)
            metadata = json.loads(
                ppt_path.with_suffix(".pptx.meta.json").read_text(encoding="utf-8")
            )
            self.assertFalse(metadata["provisional"])
            self.assertEqual(metadata["ppt_status"], "recording-ready")

    def test_old_final_metadata_is_revalidated_after_cache_schema_upgrade(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = AppConfig(data_dir=Path(directory))
            client = ClassroomClient(config)
            ppt_path = self._ppt_path(config)
            ppt_path.parent.mkdir(parents=True, exist_ok=True)
            ppt_path.write_bytes(b"old-eight-page-ppt")
            ppt_path.with_suffix(".pptx.meta.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "course_id": "course-1",
                        "lesson_id": "lesson-1",
                        "provisional": False,
                        "ppt_status": "finished",
                    }
                ),
                encoding="utf-8",
            )

            recording_detail = self._detail("running", with_recording=True)
            with (
                patch.object(client, "get_course_detail", return_value=recording_detail),
                patch.object(client, "refresh_course_detail", return_value=recording_detail),
                patch.object(client, "list_current_live_lessons", return_value=[]),
                patch.object(
                    client,
                    "_get_bytes",
                    return_value=(b"fresh-131-page-ppt", _PPT_MIME),
                ) as download,
            ):
                exported = client.download_lesson_export("course-1", "lesson-1", "ppt")

            self.assertFalse(exported.from_cache)
            self.assertEqual(ppt_path.read_bytes(), b"fresh-131-page-ppt")
            self.assertTrue(download.called)
            metadata = json.loads(
                ppt_path.with_suffix(".pptx.meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["schema"], 3)
            self.assertFalse(metadata["provisional"])
            self.assertEqual(metadata["ppt_status"], "recording-ready")

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

    def test_recording_export_stays_provisional_when_archive_has_more_pages(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = AppConfig(data_dir=Path(directory))
            client = ClassroomClient(config)
            exported_bytes = self._pptx_bytes(8)
            recording_detail = self._detail("running", with_recording=True)

            with (
                patch.object(client, "get_course_detail", return_value=recording_detail),
                patch.object(client, "refresh_course_detail", return_value=recording_detail),
                patch.object(client, "list_current_live_lessons", return_value=[]),
                patch.object(client, "list_live_ppt_slides", return_value=[object()] * 131),
                patch.object(client, "_get_bytes", return_value=(exported_bytes, _PPT_MIME)),
            ):
                exported = client.download_lesson_export("course-1", "lesson-1", "ppt")

            metadata = json.loads(
                exported.saved_path.with_suffix(".pptx.meta.json").read_text(encoding="utf-8")
            )
            self.assertFalse(exported.from_cache)
            self.assertEqual(metadata["schema"], 3)
            self.assertTrue(metadata["provisional"])
            self.assertEqual(metadata["ppt_status"], "archive-newer")


if __name__ == "__main__":
    unittest.main()
