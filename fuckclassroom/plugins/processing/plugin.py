from __future__ import annotations

from pathlib import Path

from ...core.plugins import PluginContext, PluginSpec, SettingsPanel
from .services import setup_services


PLUGIN_DIR = Path(__file__).resolve().parent


def build_routes(context: PluginContext):
    from .routes import build_router
    return build_router(context)


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="processing",
        name="课次处理",
        order=20,
        requires=("classroom",),
        service_factory=setup_services,
        route_factory=build_routes,
        template_dir=PLUGIN_DIR / "templates",
        settings_panels=(
            SettingsPanel(
                key="processing",
                label="课次处理",
                template="processing_settings.html",
                order=25,
                checkbox_fields=("auto_open_outputs_after_extract",),
            ),
        ),
    )


__all__ = ["build_plugin"]
