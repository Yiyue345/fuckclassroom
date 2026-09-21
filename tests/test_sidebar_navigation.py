from __future__ import annotations

import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from fuckclassroom.plugins import build_plugin_registry


class SidebarNavigationTests(unittest.TestCase):
    def test_navigation_is_registered_by_plugins(self) -> None:
        registry = build_plugin_registry()
        groups = {group.aria_label: group for group in registry.navigation_groups}

        self.assertEqual(
            set(groups),
            {"课程录播导航", "本科选课导航", "通用导航"},
        )
        hrefs = {
            item.href
            for group in registry.navigation_groups
            for item in group.items
        }
        self.assertEqual(
            hrefs,
            {
                "/classroom",
                "/courses",
                "/tasks",
                "/downloads",
                "/selection/info",
                "/selection/catalog",
                "/selection/info/selected",
                "/selection/auto",
                "/selection/captures",
                "/accounts",
                "/plugins",
                "/settings",
            },
        )

        template = Path("templates/base.html").read_text(encoding="utf-8")
        self.assertIn("plugin_registry.navigation_groups", template)
        self.assertNotIn('aria-label="课程录播导航"', template)
        self.assertNotIn('aria-label="本科选课导航"', template)

    def test_settings_panels_and_plugin_templates_are_registered(self) -> None:
        registry = build_plugin_registry()
        panel_keys = [panel.key for panel in registry.settings_panels]

        self.assertEqual(
            panel_keys,
            [
                "appearance",
                "courses",
                "processing",
                "auto-selection",
                "network",
                "transcription",
                "ocr",
                "ai",
                "settings-actions",
                "storage",
            ],
        )

        settings = Path("templates/settings.html").read_text(encoding="utf-8")
        base = Path("templates/base.html").read_text(encoding="utf-8")
        environment = Environment(
            loader=FileSystemLoader(
                registry.template_directories(Path("templates"))
            )
        )

        environment.get_template("base.html")
        environment.get_template("settings.html")
        environment.get_template("plugin_manager.html")
        for panel in registry.settings_panels:
            environment.get_template(panel.template)

        self.assertIn('{% set active_system = "common" %}', settings)
        self.assertIn("plugin_registry.settings_panels", settings)
        self.assertIn("plugin_registry.system_label(active_system)", base)
        self.assertEqual(registry.system_label("selection"), "本科选课")

    def test_plugin_static_mounts_are_isolated(self) -> None:
        registry = build_plugin_registry()
        mounts = {mount.plugin_id: mount for mount in registry.static_mounts}

        self.assertEqual(
            mounts["classroom"].path,
            "/plugins/classroom/static",
        )
        self.assertEqual(
            mounts["course_selection"].path,
            "/plugins/course_selection/static",
        )
        self.assertEqual(
            mounts["attendance"].path,
            "/plugins/attendance/static",
        )

    def test_session_footer_title_and_theme_button_share_the_same_track(self) -> None:
        template = Path("templates/base.html").read_text(encoding="utf-8")
        styles = Path("static/app.css").read_text(encoding="utf-8")

        self.assertIn('<span class="sidebar-footer-title">会话状态</span>', template)
        self.assertRegex(
            styles,
            r"\.sidebar-footer-head \{[^}]*height: 32px;[^}]*grid-template-columns: minmax\(0, 1fr\) 32px;",
        )
        self.assertIn(
            ".sidebar-footer-title { height: 32px; padding-left: 2px; line-height: 32px; }",
            styles,
        )
        self.assertIn(
            ".sidebar-theme-toggle.btn.icon-only { width: 32px; height: 32px; min-height: 32px; flex: 0 0 32px; }",
            styles,
        )


if __name__ == "__main__":
    unittest.main()
