"""Minimal Plugin API v1 extension: GET /sample-plugin/hello.

Avoid work at import time; the route hook runs when the plugin is enabled.
"""

from fastapi import APIRouter
from fuckclassroom.core.plugins import PluginContext, PluginSpec


def build_routes(context: PluginContext) -> APIRouter:
    router = APIRouter()

    @router.get("/sample-plugin/hello")
    def hello() -> dict[str, str]:
        return {"message": "Hello from FuckClassroom", "plugin_id": context.plugin_id}

    return router


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="sample_plugin",
        name="Sample Plugin",
        api_version=1,
        requires=("core_ui",),
        route_factory=build_routes,
    )
