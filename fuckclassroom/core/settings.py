from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "data" / "settings.json"


@dataclass(frozen=True)
class AppSettings:
    ai_base_url: str = "https://api.openai.com/v1"
    ai_api_key: str = ""
    ai_model: str = "gpt-4o-mini"
    ocr_model: str = "gpt-4o-mini"
    ocr_engine: str = "auto"
    ocr_max_images: int = 120
    auto_open_outputs_after_extract: bool = False
    default_term_id: str = ""
    hy2_enabled: bool = False
    hy2_source_dir: str = ""
    auto_selection_retry_seconds: int = 5
    auto_selection_session_retry_seconds: int = 30
    auto_selection_capacity_retry_seconds: int = 30

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        return cls(
            ai_base_url=str(data.get("ai_base_url") or cls.ai_base_url).rstrip("/"),
            ai_api_key=str(data.get("ai_api_key") or ""),
            ai_model=str(data.get("ai_model") or cls.ai_model),
            ocr_model=str(data.get("ocr_model") or data.get("ai_model") or cls.ocr_model),
            ocr_engine=_valid_ocr_engine(data.get("ocr_engine")),
            ocr_max_images=_positive_int(data.get("ocr_max_images"), cls.ocr_max_images),
            auto_open_outputs_after_extract=_to_bool(data.get("auto_open_outputs_after_extract")),
            default_term_id=str(data.get("default_term_id") or ""),
            hy2_enabled=_to_bool(data.get("hy2_enabled")),
            hy2_source_dir=str(data.get("hy2_source_dir") or "").strip(),
            auto_selection_retry_seconds=_bounded_int(
                data.get("auto_selection_retry_seconds"),
                cls.auto_selection_retry_seconds,
                5,
                3600,
            ),
            auto_selection_session_retry_seconds=_bounded_int(
                data.get("auto_selection_session_retry_seconds"),
                cls.auto_selection_session_retry_seconds,
                15,
                3600,
            ),
            auto_selection_capacity_retry_seconds=_bounded_int(
                data.get("auto_selection_capacity_retry_seconds"),
                cls.auto_selection_capacity_retry_seconds,
                15,
                3600,
            ),
        )

    def to_public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["has_ai_api_key"] = bool(self.ai_api_key)
        data["ai_api_key"] = ""
        return data


class SettingsStore:
    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self.path = path

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return AppSettings()
        if not isinstance(data, dict):
            return AppSettings()
        return AppSettings.from_dict(data)

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(asdict(settings), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def update_from_form(self, form: dict[str, str]) -> AppSettings:
        current = self.load()
        api_key = form.get("ai_api_key", "")
        if api_key == "":
            api_key = current.ai_api_key
        settings = AppSettings.from_dict(
            {
                "ai_base_url": form.get("ai_base_url", current.ai_base_url),
                "ai_api_key": api_key,
                "ai_model": form.get("ai_model", current.ai_model),
                "ocr_model": form.get("ocr_model", current.ocr_model),
                "ocr_engine": form.get("ocr_engine", current.ocr_engine),
                "ocr_max_images": form.get("ocr_max_images", current.ocr_max_images),
                "auto_open_outputs_after_extract": form.get("auto_open_outputs_after_extract"),
                "default_term_id": form.get("default_term_id", current.default_term_id),
                "hy2_enabled": form.get("hy2_enabled"),
                "auto_selection_retry_seconds": form.get(
                    "auto_selection_retry_seconds", current.auto_selection_retry_seconds
                ),
                "auto_selection_session_retry_seconds": form.get(
                    "auto_selection_session_retry_seconds",
                    current.auto_selection_session_retry_seconds,
                ),
                "auto_selection_capacity_retry_seconds": form.get(
                    "auto_selection_capacity_retry_seconds",
                    current.auto_selection_capacity_retry_seconds,
                ),
                "hy2_source_dir": form.get("hy2_source_dir", current.hy2_source_dir),
            }
        )
        self.save(settings)
        return settings


def _valid_ocr_engine(value: Any) -> str:
    engine = str(value or "auto").strip().lower()
    return engine if engine in {"auto", "rapidocr", "tesseract", "ai"} else "auto"


def _positive_int(value: Any, fallback: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return fallback


def _bounded_int(value: Any, fallback: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return fallback


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").lower() in {"1", "true", "yes", "on"}
