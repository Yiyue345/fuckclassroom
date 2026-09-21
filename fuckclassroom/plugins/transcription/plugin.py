from __future__ import annotations

from pathlib import Path

from ...core.plugins import PluginSpec, SettingsPanel
from .services import setup_services


PLUGIN_DIR = Path(__file__).resolve().parent


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="transcription",
        name="本地语音转写",
        order=30,
        requires=("processing",),
        service_factory=setup_services,
        template_dir=PLUGIN_DIR / "templates",
        settings_panels=(
            SettingsPanel(
                key="transcription",
                label="语音转写",
                template="transcription_settings.html",
                order=50,
                checkbox_fields=("asr_use_batched", "asr_simplified_chinese"),
            ),
        ),
    )


__all__ = ["build_plugin"]
