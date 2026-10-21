"""Lazily share the undergraduate academic session between feature plugins."""
from __future__ import annotations


def get_academic_session(context):
    services = context.services
    existing = services.maybe("academic_session")
    if existing is not None:
        return existing

    from ..course_selection.assistant import CourseSelectionAssistant
    from ..course_selection.proxy import Hy2ProxyManager

    proxy = Hy2ProxyManager(context.config)
    session = CourseSelectionAssistant(
        context.config,
        hy2_proxy=proxy,
        credential_store=services.get("credential_store"),
        verification_broker=services.get("verification_broker"),
        login_lock=services.get("login_lock"),
    )
    session._webvpn_session = services.maybe("webvpn_session")
    services.add("hy2_proxy", proxy)
    services.add("academic_session", session)
    return session
