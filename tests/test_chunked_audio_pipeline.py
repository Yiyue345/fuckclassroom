from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from fuckclassroom.classroom.transcription import LocalTranscriptionService
from fuckclassroom.core.config import AppConfig


class ChunkedAudioPipelineTests(unittest.TestCase):
    def test_seekable_remote_video_is_decoded_in_parallel_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "settings.json").write_text(
                json.dumps(
                    {
                        "asr_use_batched": False,
                        "asr_simplified_chinese": False,
                        "asr_max_concurrent_tasks": 1,
                    }
                ),
                encoding="utf-8",
            )
            config = AppConfig(data_dir=data_dir)
            service = LocalTranscriptionService(config)
            transcribe_calls: list[object] = []
            decode_calls: list[tuple[float, float]] = []

            class FakeModel:
                def transcribe(self, audio, **kwargs):
                    transcribe_calls.append(audio)
                    segments = iter(
                        [SimpleNamespace(start=2.0, end=4.0, text="分块课堂内容")]
                    )
                    return segments, SimpleNamespace(duration=10.0)

            def fake_decode(url, start, end, sampling_rate, cancel_check=None):
                decode_calls.append((start, end))
                return np.zeros((sampling_rate * 10,), dtype=np.float32)

            service._get_model = lambda progress=None: FakeModel()  # type: ignore[method-assign]
            target = config.outputs_dir / "课程" / "课次" / "local_transcript.txt"
            progress: list[tuple[int, str]] = []

            with patch(
                "fuckclassroom.classroom.transcription._remote_media_supports_ranges",
                return_value=True,
            ), patch(
                "fuckclassroom.classroom.transcription._decode_audio_chunk",
                side_effect=fake_decode,
            ):
                result = service.transcribe_url(
                    "https://example.test/lesson.mp4",
                    target,
                    progress=lambda percent, message: progress.append((percent, message)),
                    duration_hint_seconds=20 * 60,
                )

            self.assertEqual(len(decode_calls), 2)
            self.assertEqual(len(transcribe_calls), 2)
            self.assertTrue(all(isinstance(audio, np.ndarray) for audio in transcribe_calls))
            self.assertIn("[00:00:02 - 00:00:04] 分块课堂内容", result.text)
            self.assertIn("[00:10:00 - 00:10:02] 分块课堂内容", result.text)
            self.assertTrue(any("分块音频流水线" in message for _, message in progress))

    def test_non_seekable_server_keeps_compatible_whole_stream_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(data_dir=Path(directory))
            service = LocalTranscriptionService(config)
            observed: list[object] = []

            class FakeModel:
                def transcribe(self, audio, **kwargs):
                    observed.append(audio)
                    return (
                        iter([SimpleNamespace(start=1.0, end=2.0, text="整段流式内容")]),
                        SimpleNamespace(duration=1200.0),
                    )

            service._get_model = lambda progress=None: FakeModel()  # type: ignore[method-assign]
            target = config.outputs_dir / "课程" / "课次" / "local_transcript.txt"

            with patch(
                "fuckclassroom.classroom.transcription._remote_media_supports_ranges",
                return_value=False,
            ):
                result = service.transcribe_url(
                    "https://example.test/lesson.mp4",
                    target,
                    duration_hint_seconds=20 * 60,
                )

            self.assertEqual(observed, ["https://example.test/lesson.mp4"])
            self.assertIn("整段流式内容", result.text)


if __name__ == "__main__":
    unittest.main()
