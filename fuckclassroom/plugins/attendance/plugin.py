from __future__ import annotations

from pathlib import Path

from ...core.plugins import PluginContext, PluginSpec
from .lifecycle import startup
from .services import setup_services


PLUGIN_DIR = Path(__file__).resolve().parent


def build_routes(context: PluginContext):
    from .routes import build_router
    return build_router(context)


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="attendance",
        name="二维码签到",
        order=60,
        requires=("classroom",),
        service_factory=setup_services,
        route_factory=build_routes,
        startup=startup,
        static_dir=PLUGIN_DIR / "static",
    )


__all__ = ["build_plugin"]
