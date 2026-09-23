from pathlib import Path

from ...core.plugins import NavigationGroup, NavigationItem, PluginSpec, UIAsset


def setup_services(context):
    from .services import setup_services as setup
    setup(context)


def build_routes(context):
    from .routes import build_router
    return build_router(context)


def clear_session(context):
    if not context.services.has("course_selection"):
        context.services.get("academic_session").clear_session()


def settings_saved(context, settings, form):
    if not context.services.has("course_selection"):
        context.services.get("academic_session").invalidate_access_mode()
        if not settings.hy2_enabled:
            context.services.get("hy2_proxy").stop()


async def startup(context):
    import asyncio
    await asyncio.to_thread(context.services.get("lab_waitlist").start)


async def shutdown(context):
    import asyncio
    await asyncio.to_thread(context.services.get("lab_waitlist").stop)
    if not context.services.has("course_selection"):
        context.services.get("hy2_proxy").stop()


def build_plugin():
    return PluginSpec(
        id="lab_selection", name="实验课选课", order=21,
        service_factory=setup_services, route_factory=build_routes,
        clear_session=clear_session, settings_saved=settings_saved, startup=startup, shutdown=shutdown,
        template_dir=Path(__file__).resolve().parent / "templates",
        static_dir=Path(__file__).resolve().parent / "static",
        ui_assets=(
            UIAsset("lab_selection.js?v=20260923-2", pages=("lab_selection",)),
            UIAsset("lab_selection.css?v=20260923-2", kind="style", pages=("lab_selection",)),
        ),
        navigation_groups=(NavigationGroup(
            key="lab_selection", label="实验课选课", aria_label="实验课选课导航",
            system="lab_selection", order=21,
            items=(NavigationItem(
                key="lab_selection", label="实验课选课", href="/lab-selection",
                icon="check-square", active_keys=("lab_selection",),
            ),),
        ),),
        system_labels=(("lab_selection", "实验课选课"),),
    )
