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
        configured = os.getenv("FUCKCLASSROOM_HY2_SOURCE_DIR", self._settings().hy2_source_dir)
        if configured:
            return Path(configured).expanduser().resolve()
        return (PROJECT_ROOT.parent / "guethub" / "rust_module" / "rust").resolve()

    @property
    def hy2_runner_path(self) -> Path:
        configured = os.getenv("FUCKCLASSROOM_HY2_RUNNER_PATH")
        if configured:
            return Path(configured).expanduser().resolve()
        runner_name = "hy2_serve.exe" if os.name == "nt" else "hy2_serve"
        return self.data_dir / "hy2" / runner_name

    def _settings(self):
        return SettingsStore(self.data_dir / "settings.json").load()
