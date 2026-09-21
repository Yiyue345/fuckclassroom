from __future__ import annotations

from pathlib import Path

from ...core.plugins import PluginSpec, SettingsPanel
from .services import setup_services


PLUGIN_DIR = Path(__file__).resolve().parent


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="ocr",
        name="PPT OCR",
        order=40,
        requires=("processing",),
        service_factory=setup_services,
        template_dir=PLUGIN_DIR / "templates",
        settings_panels=(
            SettingsPanel(
                key="ocr",
                label="PPT OCR",
                template="ocr_settings.html",
                order=55,
                checkbox_fields=("ocr_cache_enabled", "ocr_use_angle_cls"),
            ),
        ),
    )


__all__ = ["build_plugin"]
