from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
