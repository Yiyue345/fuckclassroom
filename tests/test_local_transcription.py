from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fuckclassroom.classroom.client import (
    ClassroomClientError,
    Course,
    CourseDetail,
    Lesson,
    LessonResource,
)
from fuckclassroom.classroom.extraction import LessonExtractionService
from fuckclassroom.classroom.transcription import LocalTranscriptionService
from fuckclassroom.core.config import AppConfig


class LocalTranscriptionTests(unittest.TestCase):
    def test_transcribe_url_streams_segments_and_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(data_dir=Path(directory))
            service = LocalTranscriptionService(config)
            calls: list[str] = []

            class FakeModel:
                def transcribe(self, url: str, **kwargs):
                    calls.append(url)
                    segments = iter(
                        [
                            SimpleNamespace(start=1.2, end=4.7, text=" 第一段 "),
                            SimpleNamespace(start=7.0, end=10.1, text="第二段"),
                        ]
                    )
                    return segments, SimpleNamespace(duration=12.0)

            service._get_model = lambda: FakeModel()  # type: ignore[method-assign]
            target = Path(directory) / "local_transcript.txt"
            progress: list[tuple[int, str]] = []

            result = service.transcribe_url(
                "https://example.test/lesson.mp4",
                target,
                progress=lambda percent, message: progress.append((percent, message)),
            )

            self.assertFalse(result.cached)
            self.assertIn("[00:00:01 - 00:00:04] 第一段", result.text)
            self.assertIn("[00:00:07 - 00:00:10] 第二段", result.text)
            self.assertEqual(calls, ["https://example.test/lesson.mp4"])
            self.assertEqual(progress[-1][0], 100)

            service._get_model = lambda: self.fail("cached transcription should not reload the model")  # type: ignore[method-assign]
            cached = service.transcribe_url("https://example.test/lesson.mp4", target)
            self.assertTrue(cached.cached)
            self.assertEqual(cached.text, result.text)

    def test_extraction_falls_back_when_platform_transcript_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(data_dir=Path(directory))
            course = Course(
                id="course-1",
                title="测试课程",
                teacher="老师",
                type="",
                model="",
                start_at="",
                end_at="",
            )
            lesson = Lesson(
                id="lesson-1",
                title="第一课",
                status_label="已结束",
                start_at="",
                end_at="",
                duration_seconds=3600,
                lecturer="老师",
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
                    self.assert_id(course_id)
                    return detail

                @staticmethod
                def assert_id(course_id: str) -> None:
                    if course_id != "course-1":
                        raise AssertionError(course_id)

                def get_lesson_export_target(self, course_id: str, lesson_id: str, kind: str):
                    return SimpleNamespace(
                        exists=False,
                        path=Path(directory) / f"{kind}.bin",
                    )

                def download_lesson_export(self, course_id: str, lesson_id: str, kind: str):
                    raise ClassroomClientError("暂无课件内容")

            class FakeTranscription:
                def transcribe_url(self, video_url: str, output_path: Path, progress=None, force=False):
                    text = "[00:00:01 - 00:00:05] 这是本地识别出来的课堂内容"
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(text + "\n", encoding="utf-8")
                    if progress:
                        progress(100, "本地语音转写完成")
                    return SimpleNamespace(path=output_path, text=text, cached=False)

            service = LessonExtractionService(config)
            service.classroom = FakeClassroom()  # type: ignore[assignment]
            service.transcription = FakeTranscription()  # type: ignore[assignment]

            result = service.extract_lesson("course-1", "lesson-1", run_ocr=False)
            transcript = result.transcript_path.read_text(encoding="utf-8")
            combined = result.combined_path.read_text(encoding="utf-8")

            self.assertIn("本地语音转写", transcript)
            self.assertIn("这是本地识别出来的课堂内容", transcript)
            self.assertIn("这是本地识别出来的课堂内容", combined)
            self.assertTrue(any("平台转写不可用" in warning for warning in result.warnings))
            self.assertTrue(any("没有可用 PPT" in warning for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
