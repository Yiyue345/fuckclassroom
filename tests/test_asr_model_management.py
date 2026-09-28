from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from fuckclassroom.core.config import AppConfig
from fuckclassroom.plugins.transcription.engine import (
    LocalTranscriptionService,
    _transcription_slot,
)
from fuckclassroom.plugins.transcription.environment import (
    AsrRuntimeCapabilities,
    resolve_asr_runtime_config,
)
from fuckclassroom.plugins.transcription.errors import LocalTranscriptionError
from fuckclassroom.plugins.transcription.services import TranscriptionProcessProxy


class _Progress:
    def __init__(self) -> None:
        self.events: list[tuple[int, str]] = []
        self.logs: list[tuple[str, str]] = []

    def __call__(self, percent: int, message: str) -> None:
        self.events.append((percent, message))

    def log(self, message: str, level: str = "info") -> None:
        self.logs.append((level, message))


class _FakeHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], object | None]] = []

    def call_sync(self, method, params=None, *, progress=None, timeout=60):
        payload = dict(params or {})
        self.calls.append((method, payload, progress))
        if method == "transcription.model_statuses":
            return [{"model": "small", "installed": True, "size_bytes": 10, "size_label": "10 B", "path": "x"}]
        if method == "transcription.download_model":
            if progress:
                progress(45, "downloading")
            return {"model": payload["model"], "installed": True}
        if method == "transcription.delete_model":
            return {"model": payload["model"], "deleted": True}
        raise AssertionError(method)


class AsrModelManagementTests(unittest.TestCase):
    def test_model_status_reports_complete_local_snapshot_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            for name, payload in {
                "config.json": b"{}",
                "model.bin": b"123456",
                "tokenizer.json": b"{}",
                "vocabulary.json": b"abc",
            }.items():
                (snapshot / name).write_bytes(payload)

            fake_hub = ModuleType("huggingface_hub")
            fake_hub.snapshot_download = lambda *args, **kwargs: str(snapshot)  # type: ignore[attr-defined]
            service = LocalTranscriptionService(AppConfig(data_dir=root))

            with (
                patch.dict("sys.modules", {"huggingface_hub": fake_hub}),
                patch.object(service, "_validate_managed_model", return_value="small"),
                patch.object(service, "_model_repo_id", return_value="Systran/faster-whisper-small"),
            ):
                status = service.model_status("small")

            self.assertTrue(status["installed"])
            self.assertEqual(status["model"], "small")
            self.assertGreater(status["size_bytes"], 0)
            self.assertTrue(status["size_label"])

    def test_download_model_promotes_detail_progress_to_task_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = LocalTranscriptionService(AppConfig(data_dir=Path(directory)))
            progress = _Progress()

            def ensure(download_progress, *, model_name=None):
                self.assertEqual(model_name, "small")
                download_progress.detail(37, "正在下载：37%")
                return "snapshot"

            with (
                patch.object(service, "_validate_managed_model", return_value="small"),
                patch.object(service, "_ensure_model_files", side_effect=ensure),
                patch.object(
                    service,
                    "model_status",
                    return_value={
                        "model": "small",
                        "installed": True,
                        "size_bytes": 1024,
                        "size_label": "1.0 KB",
                        "path": "snapshot",
                    },
                ),
            ):
                result = service.download_model("small", progress)

            self.assertTrue(result["installed"])
            self.assertIn((37, "正在下载：37%"), progress.events)
            self.assertEqual(progress.events[-1][0], 100)

    def test_delete_model_removes_huggingface_cache_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(data_dir=Path(directory))
            service = LocalTranscriptionService(config)
            repo_dir = (
                config.asr_models_dir
                / "models--Systran--faster-whisper-small"
            )
            repo_dir.mkdir(parents=True)
            (repo_dir / "model.bin").write_bytes(b"model")

            with (
                patch.object(service, "_validate_managed_model", return_value="small"),
                patch.object(service, "_model_repo_id", return_value="Systran/faster-whisper-small"),
            ):
                result = service.delete_model("small")

            self.assertTrue(result["deleted"])
            self.assertFalse(repo_dir.exists())

    def test_delete_model_is_blocked_while_transcription_slot_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = LocalTranscriptionService(AppConfig(data_dir=Path(directory)))
            with (
                patch.object(service, "_validate_managed_model", return_value="small"),
                patch.object(service, "_model_repo_id", return_value="Systran/faster-whisper-small"),
                _transcription_slot(1, None),
            ):
                with self.assertRaisesRegex(
                    LocalTranscriptionError,
                    "当前有本地转写任务运行",
                ):
                    service.delete_model("small")

    def test_runtime_resolver_falls_back_compute_type_after_auto_selects_cpu(self) -> None:
        capabilities = AsrRuntimeCapabilities(
            cpu_compute_types=("int8", "float32", "default"),
            cuda_compute_types=(),
            cuda_available=False,
            cuda_reason="CUDA unavailable",
            cuda_device_count=0,
            auto_device="cpu",
            ctranslate2_version="test",
        )

        device, compute_type, messages = resolve_asr_runtime_config(
            "auto",
            "float16",
            capabilities=capabilities,
        )

        self.assertEqual(device, "cpu")
        self.assertEqual(compute_type, "int8")
        self.assertIn(("info", "ASR auto 已解析为 CPU"), messages)
        self.assertTrue(
            any(
                level == "warning" and "float16" in message and "int8" in message
                for level, message in messages
            )
        )

    def test_runtime_resolver_preserves_cuda_float16_when_supported(self) -> None:
        capabilities = AsrRuntimeCapabilities(
            cpu_compute_types=("int8", "float32", "default"),
            cuda_compute_types=("int8", "float16", "float32", "default"),
            cuda_available=True,
            cuda_reason="",
            cuda_device_count=1,
            auto_device="cuda",
            ctranslate2_version="test",
        )

        device, compute_type, messages = resolve_asr_runtime_config(
            "auto",
            "float16",
            capabilities=capabilities,
        )

        self.assertEqual((device, compute_type), ("cuda", "float16"))
        self.assertFalse(any(level == "warning" for level, _ in messages))

    def test_process_proxy_exposes_model_management_calls(self) -> None:
        host = _FakeHost()
        proxy = TranscriptionProcessProxy(host)  # type: ignore[arg-type]
        progress = _Progress()

        statuses = proxy.model_statuses()
        downloaded = proxy.download_model("small", progress)
        deleted = proxy.delete_model("small")

        self.assertTrue(statuses[0]["installed"])
        self.assertTrue(downloaded["installed"])
        self.assertTrue(deleted["deleted"])
        self.assertIn((45, "downloading"), progress.events)
        self.assertEqual(
            [call[0] for call in host.calls],
            [
                "transcription.model_statuses",
                "transcription.download_model",
                "transcription.delete_model",
            ],
        )


if __name__ == "__main__":
    unittest.main()
