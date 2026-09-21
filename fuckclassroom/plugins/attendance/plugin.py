from __future__ import annotations

from pathlib import Path

from ...core.plugins import AccountPanel, PluginContext, PluginSpec


PLUGIN_DIR = Path(__file__).resolve().parent

def setup_services(context: PluginContext):
    from .services import setup_services as setup
    return setup(context)

def startup(context: PluginContext):
    from .lifecycle import startup as hook
    return hook(context)


def build_routes(context: PluginContext):
    from .routes import build_router
    return build_router(context)


def build_account_context(services, request):
    from .accounts import build_account_context as build
    return build(services, request)


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="attendance",
        name="二维码签到",
        order=60,
        requires=("classroom",),
        service_factory=setup_services,
        route_factory=build_routes,
        startup=startup,
        template_dir=PLUGIN_DIR / "templates",
        account_panels=(
            AccountPanel(
                key="attendance",
                template="attendance/account_panel.html",
                order=30,
                context_factory=build_account_context,
            ),
        ),
        static_dir=PLUGIN_DIR / "static",
    )


__all__ = ["build_plugin"]
