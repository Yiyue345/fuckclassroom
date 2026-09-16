from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    def test_transcribe_url_streams_segments_simplifies_and_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "settings.json").write_text(
                json.dumps(
                    {
                        "asr_use_batched": False,
                        "asr_simplified_chinese": True,
                        "asr_max_concurrent_tasks": 2,
                    }
                ),
                encoding="utf-8",
            )
            config = AppConfig(data_dir=data_dir)
            service = LocalTranscriptionService(config)
            calls: list[str] = []

            class FakeModel:
                def transcribe(self, url: str, **kwargs):
                    calls.append(url)
                    segments = iter(
                        [
                            SimpleNamespace(start=1.2, end=4.7, text=" 軟體與資料庫 "),
                            SimpleNamespace(start=7.0, end=10.1, text="第二段"),
                        ]
                    )
                    return segments, SimpleNamespace(duration=12.0)

            service._get_model = lambda progress=None: FakeModel()  # type: ignore[method-assign]
            target = config.outputs_dir / "测试课程" / "第一课" / "local_transcript.txt"
            progress: list[tuple[int, str]] = []

            result = service.transcribe_url(
                "https://example.test/lesson.mp4",
                target,
                progress=lambda percent, message: progress.append((percent, message)),
            )

            self.assertFalse(result.cached)
            self.assertIn("[00:00:01 - 00:00:04] 软体与资料库", result.text)
            self.assertIn("[00:00:07 - 00:00:10] 第二段", result.text)
            self.assertEqual(calls, ["https://example.test/lesson.mp4"])
            self.assertTrue(
                any("00:00:10 / 00:00:12（84%）" in message for _, message in progress)
            )
            self.assertEqual(progress[-1][0], 100)
            self.assertIsNotNone(result.export_path)
            exported = config.downloads_dir / "测试课程" / "第一课" / "本地转写.txt"
            self.assertTrue(exported.exists())
            self.assertIn("软体与资料库", exported.read_text(encoding="utf-8"))

            service._get_model = lambda progress=None: self.fail("cached transcription should not reload the model")  # type: ignore[method-assign]
            cached = service.transcribe_url("https://example.test/lesson.mp4", target)
            self.assertTrue(cached.cached)
            self.assertEqual(cached.text, result.text)
            self.assertEqual(cached.export_path, exported)

    def test_batched_pipeline_uses_configured_batch_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "settings.json").write_text(
                json.dumps(
                    {
                        "asr_use_batched": True,
                        "asr_batch_size": 6,
                        "asr_simplified_chinese": False,
                        "asr_max_concurrent_tasks": 2,
                    }
                ),
                encoding="utf-8",
            )
            config = AppConfig(data_dir=data_dir)
            service = LocalTranscriptionService(config)
            fake_model = object()
            observed: dict[str, object] = {}

            class FakePipeline:
                def __init__(self, model):
                    observed["model"] = model

                def transcribe(self, url: str, **kwargs):
                    observed["url"] = url
                    observed["kwargs"] = kwargs
                    return iter([SimpleNamespace(start=0.0, end=2.0, text="测试")]), SimpleNamespace(duration=2.0)

            service._get_model = lambda progress=None: fake_model  # type: ignore[method-assign]
            target = config.outputs_dir / "课程" / "课次" / "local_transcript.txt"
            with patch("faster_whisper.BatchedInferencePipeline", FakePipeline):
                result = service.transcribe_url("https://example.test/lesson.mp4", target)

            self.assertEqual(observed["model"], fake_model)
            self.assertEqual(observed["url"], "https://example.test/lesson.mp4")
            kwargs = observed["kwargs"]
            self.assertEqual(kwargs["batch_size"], 6)
            self.assertEqual(kwargs["language"], "zh")
            self.assertIn("测试", result.text)

    def test_extraction_prefers_local_transcription_when_video_exists(self) -> None:
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
                    raise AssertionError("有录像且本地 ASR 成功时不应读取平台转写")

                def download_lesson_export(self, course_id: str, lesson_id: str, kind: str):
                    raise ClassroomClientError("有录像且本地 ASR 成功时不应下载平台转写")

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
            self.assertFalse(any("平台转写" in warning for warning in result.warnings))
            self.assertTrue(any("没有可用 PPT" in warning for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
