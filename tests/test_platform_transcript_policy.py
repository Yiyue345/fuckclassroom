from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fuckclassroom.classroom.client import Course, CourseDetail, Lesson, LessonResource
from fuckclassroom.classroom.extraction import (
    LessonExtractionService,
    _platform_transcript_is_usable,
)
from fuckclassroom.core.config import AppConfig


class PlatformTranscriptPolicyTests(unittest.TestCase):
    def test_metadata_only_platform_transcript_is_rejected(self) -> None:
        lesson = Lesson(
            id="lesson-1",
            title="2026-03-03 第1-2节",
            status_label="已结束",
            start_at="",
            end_at="",
            duration_seconds=90 * 60,
            lecturer="陈智勇",
            resources=[],
        )
        text = "计算机组成与系统结构 授课老师：陈智勇 2026-03-03第1-2节"

        usable, reason = _platform_transcript_is_usable(
            text,
            course_title="计算机组成与系统结构",
            lesson=lesson,
        )

        self.assertFalse(usable)
        self.assertIn("有效字符", reason)
        self.assertIn("最低可信阈值", reason)

    def test_recording_prefers_local_asr_without_downloading_platform_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(data_dir=Path(directory))
            course = Course(
                id="course-1",
                title="计算机组成与系统结构",
                teacher="陈智勇",
                type="",
                model="",
                start_at="",
                end_at="",
            )
            lesson = Lesson(
                id="lesson-1",
                title="2026-03-03 第1-2节",
                status_label="已结束",
                start_at="",
                end_at="",
                duration_seconds=90 * 60,
                lecturer="陈智勇",
                resources=[
                    LessonResource(
                        kind="video",
                        title="课堂录像",
                        url="https://example.test/lesson.mp4",
                    ),
                    LessonResource(
                        kind="transcript",
                        title="平台转写",
                        url="https://example.test/transcript",
                    ),
                ],
            )
            detail = CourseDetail(course=course, lessons=[lesson])

            class FakeClassroom:
                def get_course_detail(self, course_id: str):
                    self.assertEqual(course_id, "course-1")
                    return detail

                def get_lesson_export_target(self, *args, **kwargs):
                    raise AssertionError("平台转写不应该在本地 ASR 成功时被读取")

                def download_lesson_export(self, *args, **kwargs):
                    raise AssertionError("平台转写不应该在本地 ASR 成功时被下载")

                @staticmethod
                def assertEqual(left, right):
                    if left != right:
                        raise AssertionError(f"{left!r} != {right!r}")

            class FakeTranscription:
                def transcribe_url(
                    self,
                    video_url: str,
                    output_path: Path,
                    progress=None,
                    force=False,
                    duration_hint_seconds=None,
                ):
                    self.assertEqual(video_url, "https://example.test/lesson.mp4")
                    self.assertEqual(duration_hint_seconds, 90 * 60)
                    if progress:
                        progress(100, "本地语音转写完成")
                    return SimpleNamespace(
                        path=output_path,
                        text="[00:00:01 - 00:00:05] 这是本地识别出的真实课堂内容",
                        cached=False,
                        export_path=None,
                    )

                @staticmethod
                def assertEqual(left, right):
                    if left != right:
                        raise AssertionError(f"{left!r} != {right!r}")

            service = LessonExtractionService(config)
            service.classroom = FakeClassroom()  # type: ignore[assignment]
            service.transcription = FakeTranscription()  # type: ignore[assignment]

            result = service.extract_lesson("course-1", "lesson-1", run_ocr=False)
            transcript = result.transcript_path.read_text(encoding="utf-8")

            self.assertIn("本地语音转写", transcript)
            self.assertIn("真实课堂内容", transcript)
            self.assertNotIn("平台转写", transcript)


if __name__ == "__main__":
    unittest.main()
