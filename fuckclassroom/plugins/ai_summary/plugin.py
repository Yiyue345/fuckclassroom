from __future__ import annotations

from pathlib import Path

from ...core.plugins import PluginContext, PluginSpec, SettingsPanel


PLUGIN_DIR = Path(__file__).resolve().parent

def setup_services(context: PluginContext):
    from .services import setup_services as setup
    return setup(context)


def build_routes(context: PluginContext):
    from .routes import build_router
    return build_router(context)


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="ai_summary",
        name="AI 总结",
        order=50,
        requires=("processing",),
        service_factory=setup_services,
        route_factory=build_routes,
        template_dir=PLUGIN_DIR / "templates",
        settings_panels=(
            SettingsPanel(
                key="ai",
                label="AI 服务",
                template="ai_summary_settings.html",
                order=60,
                checkbox_fields=("auto_summarize_after_extract",),
            ),
        ),
    )


__all__ = ["build_plugin"]
