from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fuckclassroom.plugins.ai_summary.errors import AIClientError
from fuckclassroom.classroom.client import Course, CourseDetail, Lesson, LessonResource
from fuckclassroom.plugins.processing.extraction import LessonExtractionService
from fuckclassroom.core.config import AppConfig


class AutomaticAiSummaryTests(unittest.TestCase):
    def _build_service(self, data_dir: Path, *, ai) -> LessonExtractionService:
        (data_dir / "settings.json").write_text(
            json.dumps(
                {
                    "ai_api_key": "test-key",
                    "ai_model": "test-model",
                    "auto_summarize_after_extract": True,
                }
            ),
            encoding="utf-8",
        )
        config = AppConfig(data_dir=data_dir)
        course = Course(
            id="course-1",
            title="算法设计与分析",
            teacher="Teacher",
            type="",
            model="",
            start_at="",
            end_at="",
        )
        lesson = Lesson(
            id="lesson-1",
            title="第 1 次课",
            status_label="已结束",
            start_at="",
            end_at="",
            duration_seconds=3600,
            lecturer="Teacher",
            resources=[
                LessonResource(
                    kind="video",
                    title="课堂录像",
                    url="https://example.test/lesson.mp4",
                ),
                LessonResource(
                    kind="ppt",
                    title="课堂课件",
                    url="https://example.test/slides.pptx",
                ),
            ],
        )
        detail = CourseDetail(course=course, lessons=[lesson])

        class FakeClassroom:
            def get_course_detail(self, course_id: str):
                if course_id != "course-1":
                    raise AssertionError(course_id)
                return detail

        class FakeTranscription:
            def transcribe_url(
                self,
                video_url: str,
                output_path: Path,
                progress=None,
                force=False,
                duration_hint_seconds=None,
            ):
                if video_url != "https://example.test/lesson.mp4":
                    raise AssertionError(video_url)
                if progress:
                    progress(100, "本地语音转写完成")
                return SimpleNamespace(
                    path=output_path,
                    text="[00:00:01 - 00:00:04] 动态规划通过状态转移复用子问题结果",
                    cached=False,
                    export_path=None,
                )

        service = LessonExtractionService(config)
        service.classroom = FakeClassroom()  # type: ignore[assignment]
        service.transcription = FakeTranscription()  # type: ignore[assignment]
        service.ai = ai  # type: ignore[assignment]
        return service

    def test_extract_lesson_sends_video_and_ppt_text_to_ai_and_saves_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            captured: dict[str, str] = {}

            class FakeAI:
                def summarize_course_text(self, text: str) -> str:
                    captured["text"] = text
                    return "本节课介绍动态规划的状态定义、状态转移与复杂度分析。"

            service = self._build_service(data_dir, ai=FakeAI())
            ppt_path = data_dir / "slides.pptx"
            ppt_path.write_bytes(b"fake-pptx")

            with (
                patch.object(service, "_download_export", return_value=(ppt_path, True)),
                patch(
                    "fuckclassroom.plugins.processing.extraction.extract_pptx_text",
                    return_value=("PPT：状态转移方程 dp[i] = min(dp[i-1], dp[i-2])", []),
                ),
            ):
                result = service.extract_lesson("course-1", "lesson-1", run_ocr=False)

            self.assertIsNotNone(result.summary_path)
            self.assertTrue(result.summary_path.exists())
            self.assertIn("AI 课程总结", result.summary_path.read_text(encoding="utf-8"))
            self.assertIn("动态规划的状态定义", result.summary_path.read_text(encoding="utf-8"))
            self.assertIn("动态规划通过状态转移复用子问题结果", captured["text"])
            self.assertIn("PPT：状态转移方程", captured["text"])

    def test_ai_failure_keeps_extracted_text_and_reports_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)

            class FailingAI:
                def summarize_course_text(self, text: str) -> str:
                    raise AIClientError("mock provider unavailable")

            service = self._build_service(data_dir, ai=FailingAI())
            ppt_path = data_dir / "slides.pptx"
            ppt_path.write_bytes(b"fake-pptx")

            with (
                patch.object(service, "_download_export", return_value=(ppt_path, True)),
                patch(
                    "fuckclassroom.plugins.processing.extraction.extract_pptx_text",
                    return_value=("PPT 文字内容", []),
                ),
            ):
                result = service.extract_lesson("course-1", "lesson-1", run_ocr=False)

            self.assertTrue(result.combined_path.exists())
            self.assertIsNone(result.summary_path)
            self.assertTrue(any("AI 自动总结失败" in item for item in result.warnings))


if __name__ == "__main__":
    unittest.main()
