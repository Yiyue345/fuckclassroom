from __future__ import annotations

import re
import unittest
from pathlib import Path


class LoginStatusIndicatorTests(unittest.TestCase):
    def test_sidebar_and_topbar_expose_each_session_indicator(self) -> None:
        base = Path("templates/base.html").read_text(encoding="utf-8")
        plugin_templates = "\n".join(
            path.read_text(encoding="utf-8")
            for path in Path("fuckclassroom/plugins").rglob("*.html")
        )

        self.assertIn("plugin_registry.sidebar_templates", base)
        self.assertIn("plugin_registry.topbar_status_templates", base)
        self.assertEqual(plugin_templates.count('data-session-indicator="classroom"'), 3)
        self.assertEqual(plugin_templates.count('data-session-indicator="selection"'), 3)
        self.assertIn('data-session-label="classroom"', plugin_templates)
        self.assertIn('data-session-label="selection"', plugin_templates)

    def test_only_login_actions_activate_the_transient_state(self) -> None:
        template = Path("fuckclassroom/plugins/core_ui/templates/core/accounts.html").read_text(encoding="utf-8")

        self.assertRegex(
            template,
            r'<form[^>]+action="/auth/start"[^>]+data-session-login="classroom"',
        )
        self.assertRegex(
            template,
            r'<form[^>]+action="/selection/login"[^>]+data-session-login="selection"',
        )
        self.assertNotRegex(
            template,
            r'<form[^>]+action="/(?:auth/verify|selection/open(?:-course-select)?)"[^>]+data-session-login=',
        )

    def test_login_task_uses_blue_state_and_restores_it_after_failure(self) -> None:
        script = Path("static/app.js").read_text(encoding="utf-8")
        styles = Path("static/app.css").read_text(encoding="utf-8")

        self.assertIn("function markSessionLoggingIn(system)", script)
        self.assertIn('label.textContent = "登录中"', script)
        self.assertIn("if (loginSystem) markSessionLoggingIn(loginSystem);", script)
        self.assertIn("if (loginSystem && !taskSucceeded) restoreSessionState(loginSystem);", script)
        self.assertIn(".session-dot.logging-in", styles)
        self.assertIn("background: var(--primary)", styles)


if __name__ == "__main__":
    unittest.main()
