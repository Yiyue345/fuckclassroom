from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from fuckclassroom.core.config import AppConfig
from fuckclassroom.plugins.transcription.engine import (
    LocalTranscriptionService,
    _MODEL_CACHE,
    _TRANSCRIPTION_CONDITION,
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

    def test_delete_model_blocks_new_transcription_until_maintenance_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(data_dir=Path(directory))
            service = LocalTranscriptionService(config)
            repo_id = "Systran/faster-whisper-small"
            repo_dir = config.asr_models_dir / "models--Systran--faster-whisper-small"
            repo_dir.mkdir(parents=True)
            (repo_dir / "model.bin").write_bytes(b"model")

            delete_in_progress = threading.Event()
            allow_delete = threading.Event()
            transcription_entered = threading.Event()
            delete_errors: list[BaseException] = []
            original_rmtree = shutil.rmtree

            def blocking_rmtree(path, *args, **kwargs):
                if Path(path) == repo_dir:
                    delete_in_progress.set()
                    if not allow_delete.wait(timeout=3):
                        raise RuntimeError("test timed out waiting to finish deletion")
                return original_rmtree(path, *args, **kwargs)

            def delete_worker() -> None:
                try:
                    service.delete_model("small")
                except BaseException as exc:  # noqa: BLE001 - surfaced in assertion below.
                    delete_errors.append(exc)

            def transcription_worker() -> None:
                with _transcription_slot(1, None):
                    transcription_entered.set()

            with (
                patch.object(service, "_validate_managed_model", return_value="small"),
                patch.object(service, "_model_repo_id", return_value=repo_id),
                patch.object(service, "_model_aliases_for_repo", return_value=("small",)),
                patch(
                    "fuckclassroom.plugins.transcription.engine.shutil.rmtree",
                    side_effect=blocking_rmtree,
                ),
            ):
                delete_thread = threading.Thread(target=delete_worker)
                delete_thread.start()
                self.assertTrue(delete_in_progress.wait(timeout=2))

                transcription_thread = threading.Thread(target=transcription_worker)
                transcription_thread.start()
                self.assertFalse(transcription_entered.wait(timeout=0.1))

                allow_delete.set()
                delete_thread.join(timeout=2)
                self.assertFalse(delete_thread.is_alive())
                self.assertFalse(delete_errors)

                self.assertTrue(transcription_entered.wait(timeout=2))
                transcription_thread.join(timeout=2)
                self.assertFalse(transcription_thread.is_alive())

    def test_second_delete_rechecks_active_transcription_after_waiting_for_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(data_dir=Path(directory))
            service = LocalTranscriptionService(config)
            repo_a = "example/model-a"
            repo_b = "example/model-b"
            repo_dirs = {
                repo_a: config.asr_models_dir / "models--example--model-a",
                repo_b: config.asr_models_dir / "models--example--model-b",
            }
            for repo_dir in repo_dirs.values():
                repo_dir.mkdir(parents=True)
                (repo_dir / "model.bin").write_bytes(b"model")

            delete_a_blocked = threading.Event()
            allow_delete_a = threading.Event()
            delete_b_woke = threading.Event()
            allow_delete_b_recheck = threading.Event()
            delete_b_rmtree_entered = threading.Event()
            transcription_entered = threading.Event()
            allow_transcription_exit = threading.Event()
            delete_a_errors: list[BaseException] = []
            delete_b_errors: list[BaseException] = []
            original_rmtree = shutil.rmtree
            original_condition_wait = _TRANSCRIPTION_CONDITION.wait

            def repo_for_model(model_name: str) -> str:
                return repo_a if model_name == "model-a" else repo_b

            def blocking_rmtree(path, *args, **kwargs):
                target = Path(path)
                if target == repo_dirs[repo_a]:
                    delete_a_blocked.set()
                    if not allow_delete_a.wait(timeout=3):
                        raise RuntimeError("test timed out waiting to finish delete A")
                elif target == repo_dirs[repo_b]:
                    delete_b_rmtree_entered.set()
                return original_rmtree(path, *args, **kwargs)

            def coordinated_condition_wait(timeout=None):
                result = original_condition_wait(timeout=timeout)
                if threading.current_thread().name == "delete-b":
                    # The Condition.wait contract returns with the condition lock
                    # reacquired. Temporarily release it here so the already
                    # waiting transcription can enter its slot before delete B
                    # resumes after delete A's maintenance window.
                    _TRANSCRIPTION_CONDITION.release()
                    try:
                        delete_b_woke.set()
                        if not allow_delete_b_recheck.wait(timeout=3):
                            raise RuntimeError(
                                "test timed out waiting to resume delete B"
                            )
                    finally:
                        _TRANSCRIPTION_CONDITION.acquire()
                return result

            def delete_worker(model_name: str, errors: list[BaseException]) -> None:
                try:
                    service.delete_model(model_name)
                except BaseException as exc:  # noqa: BLE001 - asserted below.
                    errors.append(exc)

            def transcription_worker() -> None:
                with _transcription_slot(1, None):
                    transcription_entered.set()
                    allow_transcription_exit.wait(timeout=3)

            with (
                patch.object(
                    service,
                    "_validate_managed_model",
                    side_effect=lambda model_name: model_name,
                ),
                patch.object(
                    service,
                    "_model_repo_id",
                    side_effect=repo_for_model,
                ),
                patch.object(
                    service,
                    "_model_aliases_for_repo",
                    side_effect=lambda repo_id: (
                        "model-a" if repo_id == repo_a else "model-b",
                    ),
                ),
                patch(
                    "fuckclassroom.plugins.transcription.engine.shutil.rmtree",
                    side_effect=blocking_rmtree,
                ),
                patch.object(
                    _TRANSCRIPTION_CONDITION,
                    "wait",
                    side_effect=coordinated_condition_wait,
                ),
            ):
                delete_a = threading.Thread(
                    target=delete_worker,
                    args=("model-a", delete_a_errors),
                    name="delete-a",
                )
                delete_a.start()
                self.assertTrue(delete_a_blocked.wait(timeout=2))

                delete_b = threading.Thread(
                    target=delete_worker,
                    args=("model-b", delete_b_errors),
                    name="delete-b",
                )
                delete_b.start()
                self.assertFalse(delete_b_rmtree_entered.wait(timeout=0.1))
                self.assertTrue(delete_b.is_alive())

                transcription = threading.Thread(
                    target=transcription_worker,
                    name="transcription",
                )
                transcription.start()
                self.assertFalse(transcription_entered.wait(timeout=0.1))

                allow_delete_a.set()
                self.assertTrue(delete_b_woke.wait(timeout=2))
                self.assertTrue(transcription_entered.wait(timeout=2))

                allow_delete_b_recheck.set()
                delete_b.join(timeout=2)
                self.assertFalse(delete_b.is_alive())
                self.assertFalse(delete_b_rmtree_entered.is_set())
                self.assertEqual(len(delete_b_errors), 1)
                self.assertIsInstance(delete_b_errors[0], LocalTranscriptionError)
                self.assertIn("当前有本地转写任务运行", str(delete_b_errors[0]))

                allow_transcription_exit.set()
                transcription.join(timeout=2)
                delete_a.join(timeout=2)

            self.assertFalse(transcription.is_alive())
            self.assertFalse(delete_a.is_alive())
            self.assertFalse(delete_a_errors)

    def test_delete_rejection_does_not_leave_model_maintenance_enabled(self) -> None:
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

            entered = False
            with _transcription_slot(1, None):
                entered = True
            self.assertTrue(entered)

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

    def test_get_model_uses_resolved_runtime_and_logs_effective_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(data_dir=Path(directory))
            service = LocalTranscriptionService(config)
            progress = _Progress()
            captured = {}

            class FakeWhisperModel:
                def __init__(self, model_path, **kwargs):
                    captured["model_path"] = model_path
                    captured.update(kwargs)

            fake_whisper = ModuleType("faster_whisper")
            fake_whisper.WhisperModel = FakeWhisperModel  # type: ignore[attr-defined]
            capabilities = AsrRuntimeCapabilities(
                cpu_compute_types=("int8", "float32", "default"),
                cuda_compute_types=(),
                cuda_available=False,
                cuda_reason="CUDA unavailable",
                cuda_device_count=0,
                auto_device="cpu",
                ctranslate2_version="test",
            )

            _MODEL_CACHE.clear()
            try:
                with (
                    patch.dict(
                        "os.environ",
                        {
                            "FUCKCLASSROOM_ASR_DEVICE": "auto",
                            "FUCKCLASSROOM_ASR_COMPUTE_TYPE": "float16",
                        },
                    ),
                    patch.dict("sys.modules", {"faster_whisper": fake_whisper}),
                    patch(
                        "fuckclassroom.plugins.transcription.environment.detect_asr_runtime",
                        return_value=capabilities,
                    ),
                    patch.object(service, "_model_cache_identity", return_value="repo:test/model"),
                    patch.object(service, "_ensure_model_files", return_value="snapshot"),
                ):
                    service._get_model(progress)
                    self.assertEqual(config.asr_device, "auto")
                    self.assertEqual(config.asr_compute_type, "float16")
            finally:
                _MODEL_CACHE.clear()

            self.assertEqual(captured["device"], "cpu")
            self.assertEqual(captured["compute_type"], "int8")
            self.assertTrue(
                any(
                    "device=cpu" in message and "compute_type=int8" in message
                    for _, message in progress.logs
                )
            )

    def test_delete_alias_releases_all_cached_models_for_shared_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(data_dir=Path(directory))
            service = LocalTranscriptionService(config)
            repo_id = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
            repo_dir = (
                config.asr_models_dir
                / "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
            )
            repo_dir.mkdir(parents=True)
            (repo_dir / "model.bin").write_bytes(b"model")
            _MODEL_CACHE.clear()
            _MODEL_CACHE[("repo:" + repo_id, "cuda", "float16", 1, 1, "x")] = object()
            _MODEL_CACHE[("turbo", "cuda", "float16", 1, 1, "x")] = object()
            _MODEL_CACHE[("large-v3-turbo", "cpu", "int8", 1, 1, "x")] = object()

            try:
                with (
                    patch.object(service, "_validate_managed_model", return_value="turbo"),
                    patch.object(service, "_model_repo_id", return_value=repo_id),
                    patch.object(
                        service,
                        "_model_aliases_for_repo",
                        return_value=("turbo", "large-v3-turbo"),
                    ),
                ):
                    result = service.delete_model("turbo")
                remaining = dict(_MODEL_CACHE)
            finally:
                _MODEL_CACHE.clear()

            self.assertTrue(result["deleted"])
            self.assertEqual(result["repo_id"], repo_id)
            self.assertEqual(set(result["aliases"]), {"turbo", "large-v3-turbo"})
            self.assertEqual(remaining, {})
            self.assertFalse(repo_dir.exists())

    def test_model_status_exposes_shared_repo_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.json"):
                (snapshot / name).write_bytes(b"x")

            fake_hub = ModuleType("huggingface_hub")
            fake_hub.snapshot_download = lambda *args, **kwargs: str(snapshot)  # type: ignore[attr-defined]
            service = LocalTranscriptionService(AppConfig(data_dir=root))
            repo_id = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"

            with (
                patch.dict("sys.modules", {"huggingface_hub": fake_hub}),
                patch.object(service, "_validate_managed_model", return_value="turbo"),
                patch.object(service, "_model_repo_id", return_value=repo_id),
                patch.object(
                    service,
                    "_model_aliases_for_repo",
                    return_value=("turbo", "large-v3-turbo"),
                ),
            ):
                status = service.model_status("turbo")

            self.assertTrue(status["installed"])
            self.assertEqual(status["repo_id"], repo_id)
            self.assertEqual(
                set(status["aliases"]),
                {"turbo", "large-v3-turbo"},
            )

    def test_model_statuses_keep_aliases_in_sync_for_shared_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.json"):
                (snapshot / name).write_bytes(b"x")

            fake_hub = ModuleType("huggingface_hub")
            fake_hub.snapshot_download = lambda *args, **kwargs: str(snapshot)  # type: ignore[attr-defined]
            service = LocalTranscriptionService(AppConfig(data_dir=root))
            repo_id = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"

            with (
                patch.dict("sys.modules", {"huggingface_hub": fake_hub}),
                patch(
                    "fuckclassroom.plugins.transcription.environment.available_asr_models",
                    return_value=("turbo", "large-v3-turbo"),
                ),
                patch.object(service, "_model_repo_id", return_value=repo_id),
            ):
                statuses = service.model_statuses()

            self.assertEqual(
                [status["model"] for status in statuses],
                ["turbo", "large-v3-turbo"],
            )
            self.assertTrue(all(status["installed"] for status in statuses))
            self.assertTrue(all(status["repo_id"] == repo_id for status in statuses))
            self.assertTrue(
                all(
                    set(status["aliases"]) == {"turbo", "large-v3-turbo"}
                    for status in statuses
                )
            )

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
