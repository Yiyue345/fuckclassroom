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
    ocr_workers: int = 4
    ocr_onnx_threads: int = 1
    ocr_max_side: int = 1600
    ocr_use_angle_cls: bool = False
    ocr_cache_enabled: bool = True
    asr_model: str = "small"
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"
    asr_cpu_threads: int = 4
    asr_num_workers: int = 2
    asr_max_concurrent_tasks: int = 2
    audio_prefetch_queue_size: int = 3
    asr_batch_size: int = 8
    asr_use_batched: bool = True
    asr_simplified_chinese: bool = True
    auto_open_outputs_after_extract: bool = False
    default_term_id: str = ""
    hy2_enabled: bool = False
    auto_selection_retry_seconds: int = 5
    auto_selection_session_retry_seconds: int = 30
    auto_selection_capacity_retry_seconds: int = 30
    catalog_lesson_name_targets: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        return cls(
            ai_base_url=str(data.get("ai_base_url") or cls.ai_base_url).rstrip("/"),
            ai_api_key=str(data.get("ai_api_key") or ""),
            ai_model=str(data.get("ai_model") or cls.ai_model),
            ocr_model=str(data.get("ocr_model") or data.get("ai_model") or cls.ocr_model),
            ocr_engine=_valid_ocr_engine(data.get("ocr_engine")),
            ocr_max_images=_positive_int(data.get("ocr_max_images"), cls.ocr_max_images),
            ocr_workers=_bounded_int(data.get("ocr_workers"), cls.ocr_workers, 1, 16),
            ocr_onnx_threads=_bounded_int(
                data.get("ocr_onnx_threads"), cls.ocr_onnx_threads, 1, 16
            ),
            ocr_max_side=_bounded_int(data.get("ocr_max_side"), cls.ocr_max_side, 512, 4096),
            ocr_use_angle_cls=_to_bool_default(
                data.get("ocr_use_angle_cls"), cls.ocr_use_angle_cls
            ),
            ocr_cache_enabled=_to_bool_default(
                data.get("ocr_cache_enabled"), cls.ocr_cache_enabled
            ),
            asr_model=str(data.get("asr_model") or cls.asr_model).strip() or cls.asr_model,
            asr_device=_valid_asr_device(data.get("asr_device")),
            asr_compute_type=str(data.get("asr_compute_type") or cls.asr_compute_type).strip().lower(),
            asr_cpu_threads=_bounded_int(data.get("asr_cpu_threads"), cls.asr_cpu_threads, 1, 64),
            asr_num_workers=_bounded_int(data.get("asr_num_workers"), cls.asr_num_workers, 1, 16),
            asr_max_concurrent_tasks=_bounded_int(
                data.get("asr_max_concurrent_tasks"), cls.asr_max_concurrent_tasks, 1, 8
            ),
            audio_prefetch_queue_size=_bounded_int(
                data.get("audio_prefetch_queue_size"),
                cls.audio_prefetch_queue_size,
                1,
                8,
            ),
            asr_batch_size=_bounded_int(data.get("asr_batch_size"), cls.asr_batch_size, 1, 32),
            asr_use_batched=_to_bool_default(data.get("asr_use_batched"), cls.asr_use_batched),
            asr_simplified_chinese=_to_bool_default(
                data.get("asr_simplified_chinese"), cls.asr_simplified_chinese
            ),
            auto_open_outputs_after_extract=_to_bool(data.get("auto_open_outputs_after_extract")),
            default_term_id=str(data.get("default_term_id") or ""),
            hy2_enabled=_to_bool(data.get("hy2_enabled")),
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
            catalog_lesson_name_targets=_lesson_name_targets(
                data.get("catalog_lesson_name_targets")
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
                "ocr_workers": form.get("ocr_workers", current.ocr_workers),
                "ocr_onnx_threads": form.get("ocr_onnx_threads", current.ocr_onnx_threads),
                "ocr_max_side": form.get("ocr_max_side", current.ocr_max_side),
                "ocr_use_angle_cls": form.get("ocr_use_angle_cls", ""),
                "ocr_cache_enabled": form.get("ocr_cache_enabled", ""),
                "asr_model": form.get("asr_model", current.asr_model),
                "asr_device": form.get("asr_device", current.asr_device),
                "asr_compute_type": form.get("asr_compute_type", current.asr_compute_type),
                "asr_cpu_threads": form.get("asr_cpu_threads", current.asr_cpu_threads),
                "asr_num_workers": form.get("asr_num_workers", current.asr_num_workers),
                "asr_max_concurrent_tasks": form.get(
                    "asr_max_concurrent_tasks", current.asr_max_concurrent_tasks
                ),
                "audio_prefetch_queue_size": form.get(
                    "audio_prefetch_queue_size", current.audio_prefetch_queue_size
                ),
                "asr_batch_size": form.get("asr_batch_size", current.asr_batch_size),
                "asr_use_batched": form.get("asr_use_batched", ""),
                "asr_simplified_chinese": form.get("asr_simplified_chinese", ""),
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
                "catalog_lesson_name_targets": form.get(
                    "catalog_lesson_name_targets",
                    current.catalog_lesson_name_targets,
                ),
            }
        )
        self.save(settings)
        return settings


def _lesson_name_targets(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        values = value
    else:
        values = str(value or "").replace("，", ",").replace("；", ",").replace(";", ",")
        values = values.replace("\r", "\n").replace(",", "\n").split("\n")
    targets: list[str] = []
    for item in values:
        target = " ".join(str(item or "").split())
        if target and target not in targets:
            targets.append(target[:80])
        if len(targets) >= 20:
            break
    return tuple(targets)


def _valid_ocr_engine(value: Any) -> str:
    engine = str(value or "auto").strip().lower()
    return engine if engine in {"auto", "rapidocr", "tesseract", "ai"} else "auto"


def _valid_asr_device(value: Any) -> str:
    device = str(value or "cpu").strip().lower()
    return device if device in {"cpu", "cuda", "auto"} else "cpu"


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


def _to_bool_default(value: Any, fallback: bool) -> bool:
    if value is None:
        return fallback
    return _to_bool(value)
