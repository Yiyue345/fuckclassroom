from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .settings import SettingsStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AppConfig:
    site_url: str = "https://classroom.guet.edu.cn/?tenant_code=21"
    data_dir: Path = PROJECT_ROOT / "data"
    auth_dir_name: str = "auth"
    login_timeout_seconds: int = 600
    browser_headless: bool = False

    @property
    def auth_dir(self) -> Path:
        return self.data_dir / self.auth_dir_name

    @property
    def storage_state_path(self) -> Path:
        return self.auth_dir / "storage_state.json"

    @property
    def session_meta_path(self) -> Path:
        return self.auth_dir / "session_meta.json"

    @property
    def login_status_path(self) -> Path:
        return self.auth_dir / "login_status.json"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def asr_models_dir(self) -> Path:
        return self.data_dir / "models" / "faster-whisper"

    @property
    def ai_base_url(self) -> str:
        return os.getenv("FUCKCLASSROOM_AI_BASE_URL", self._settings().ai_base_url).rstrip("/")

    @property
    def ai_api_key(self) -> str:
        return os.getenv("FUCKCLASSROOM_AI_API_KEY", self._settings().ai_api_key)

    @property
    def ai_model(self) -> str:
        return os.getenv("FUCKCLASSROOM_AI_MODEL", self._settings().ai_model)

    @property
    def ocr_model(self) -> str:
        return os.getenv("FUCKCLASSROOM_OCR_MODEL", self._settings().ocr_model or self.ai_model)

    @property
    def ocr_engine(self) -> str:
        return os.getenv("FUCKCLASSROOM_OCR_ENGINE", self._settings().ocr_engine).lower()

    @property
    def ocr_max_images(self) -> int:
        raw = os.getenv("FUCKCLASSROOM_OCR_MAX_IMAGES", str(self._settings().ocr_max_images))
        try:
            return max(1, int(raw))
        except ValueError:
            return self._settings().ocr_max_images

    @property
    def asr_model(self) -> str:
        return os.getenv("FUCKCLASSROOM_ASR_MODEL", self._settings().asr_model).strip() or "small"

    @property
    def asr_device(self) -> str:
        return os.getenv("FUCKCLASSROOM_ASR_DEVICE", self._settings().asr_device).strip().lower() or "cpu"

    @property
    def asr_compute_type(self) -> str:
        configured = self._settings().asr_compute_type
        return os.getenv("FUCKCLASSROOM_ASR_COMPUTE_TYPE", configured).strip().lower() or configured

    @property
    def asr_language(self) -> str:
        return os.getenv("FUCKCLASSROOM_ASR_LANGUAGE", "zh").strip()

    @property
    def asr_cpu_threads(self) -> int:
        return self._env_int("FUCKCLASSROOM_ASR_CPU_THREADS", self._settings().asr_cpu_threads, 1, 64)

    @property
    def asr_num_workers(self) -> int:
        return self._env_int("FUCKCLASSROOM_ASR_NUM_WORKERS", self._settings().asr_num_workers, 1, 16)

    @property
    def asr_max_concurrent_tasks(self) -> int:
        return self._env_int(
            "FUCKCLASSROOM_ASR_MAX_CONCURRENT_TASKS",
            self._settings().asr_max_concurrent_tasks,
            1,
            8,
        )

    @property
    def asr_batch_size(self) -> int:
        return self._env_int("FUCKCLASSROOM_ASR_BATCH_SIZE", self._settings().asr_batch_size, 1, 32)

    @property
    def asr_use_batched(self) -> bool:
        return self._env_bool("FUCKCLASSROOM_ASR_USE_BATCHED", self._settings().asr_use_batched)

    @property
    def asr_simplified_chinese(self) -> bool:
        return self._env_bool(
            "FUCKCLASSROOM_ASR_SIMPLIFIED_CHINESE",
            self._settings().asr_simplified_chinese,
        )

    @property
    def auto_open_outputs_after_extract(self) -> bool:
        raw = os.getenv("FUCKCLASSROOM_AUTO_OPEN_OUTPUTS_AFTER_EXTRACT")
        if raw is not None:
            return raw.lower() in {"1", "true", "yes", "on"}
        return self._settings().auto_open_outputs_after_extract

    @property
    def default_term_id(self) -> str:
        return os.getenv("FUCKCLASSROOM_DEFAULT_TERM_ID", self._settings().default_term_id)

    @property
    def hy2_enabled(self) -> bool:
        raw = os.getenv("FUCKCLASSROOM_HY2_ENABLED")
        if raw is not None:
            return raw.lower() in {"1", "true", "yes", "on"}
        return self._settings().hy2_enabled

    @property
    def hy2_source_dir(self) -> Path:
        return PROJECT_ROOT / "rust_module" / "hy2_proxy"

    @property
    def hy2_runner_path(self) -> Path:
        runner_name = "hy2_serve.exe" if os.name == "nt" else "hy2_serve"
        return self.data_dir / "hy2" / runner_name

    def _settings(self):
        return SettingsStore(self.data_dir / "settings.json").load()

    @staticmethod
    def _env_int(name: str, fallback: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.getenv(name, str(fallback)))
        except ValueError:
            value = fallback
        return max(minimum, min(maximum, value))

    @staticmethod
    def _env_bool(name: str, fallback: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return fallback
        return raw.lower() in {"1", "true", "yes", "on"}
