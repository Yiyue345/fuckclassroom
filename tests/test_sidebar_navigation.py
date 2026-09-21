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

        by_key = {group.key: group for group in registry.navigation_groups}
        self.assertIn("/tasks", {item.href for item in by_key["common"].items})
        self.assertNotIn("/tasks", {item.href for item in by_key["classroom"].items})

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

        settings = Path("fuckclassroom/plugins/core_ui/templates/core/settings.html").read_text(encoding="utf-8")
        base = Path("templates/base.html").read_text(encoding="utf-8")
        environment = Environment(
            loader=FileSystemLoader(
                registry.template_directories(Path("templates"))
            )
        )

        environment.get_template("base.html")
        environment.get_template("core/settings.html")
        environment.get_template("core/plugin_manager.html")
        for panel in registry.settings_panels:
            environment.get_template(panel.template)

        self.assertIn('{% set active_system = "common" %}', settings)
        self.assertIn("settings_panels", settings)
        self.assertNotIn("plugin_registry.settings_panels", settings)

        panels = {panel.key: panel for panel in registry.settings_panels}
        self.assertIsNotNone(panels["courses"].context_factory)
        self.assertIsNotNone(panels["network"].context_factory)
        self.assertIsNotNone(panels["storage"].context_factory)
        self.assertIn("plugin_registry.system_label(active_system)", base)
        self.assertEqual(registry.system_label("selection"), "本科选课")

    def test_account_panels_are_registered_by_plugins(self) -> None:
        registry = build_plugin_registry()
        panels = {panel.key: panel for panel in registry.account_panels}

        self.assertEqual(
            list(panels),
            ["classroom", "course_selection", "attendance"],
        )
        self.assertEqual(panels["classroom"].template, "classroom/account_panel.html")
        self.assertEqual(panels["course_selection"].template, "selection/account_panel.html")
        self.assertEqual(panels["attendance"].template, "attendance/account_panel.html")

        environment = Environment(
            loader=FileSystemLoader(registry.template_directories(Path("templates")))
        )
        for panel in registry.account_panels:
            environment.get_template(panel.template)

        classroom_only = build_plugin_registry({"classroom"})
        self.assertEqual(
            [panel.key for panel in classroom_only.account_panels],
            ["classroom"],
        )

    def test_plugin_manager_reuses_existing_card_spacing(self) -> None:
        template = Path(
            "fuckclassroom/plugins/core_ui/templates/core/plugin_manager.html"
        ).read_text(encoding="utf-8")
        styles = Path("fuckclassroom/plugins/core_ui/static/core_ui.css").read_text(encoding="utf-8")
        base = Path("templates/base.html").read_text(encoding="utf-8")

        self.assertIn("account-session-panel plugin-manager-card", template)
        self.assertIn("account-session-header plugin-manager-card-header", template)
        self.assertIn("detail-grid plugin-manager-details", template)
        self.assertIn(
            ".account-session-panel { display: flex; min-width: 0; flex-direction: column; scroll-margin-top: 92px; padding: 22px; }",
            styles,
        )
        self.assertIn(
            ".plugin-install-option { display: flex; min-width: 0; flex-direction: column; border: 1px solid var(--border); border-radius: 9px; background: var(--surface-subtle); padding: 18px; }",
            styles,
        )
        self.assertIn(
            ".plugin-manager-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; align-items: stretch; }",
            styles,
        )
        self.assertIn('class="toggle-control plugin-toggle-control"', template)
        self.assertIn('role="switch"', template)
        self.assertIn('onchange="this.form.requestSubmit()"', template)
        self.assertNotIn("plugin-toggle-state", template)
        self.assertNotIn("account-session-actions plugin-manager-actions", template)
        self.assertNotIn(">停用</button>", template)
        self.assertNotIn(">启用</button>", template)
        self.assertIn("/static/app.css?v=20260921-5", base)

    def test_plugin_ui_assets_are_registered_by_enabled_plugins(self) -> None:
        registry = build_plugin_registry()

        self.assertIn(
            "/plugins/core_ui/static/core_ui.css?v=20260921-2",
            registry.stylesheets,
        )
        self.assertIn(
            "/plugins/classroom/static/classroom.css?v=20260921-1",
            registry.stylesheets,
        )
        self.assertIn(
            "/plugins/attendance/static/attendance.css?v=20260921-1",
            registry.stylesheets,
        )
        self.assertIn(
            "/plugins/processing/static/processing.css?v=20260921-1",
            registry.stylesheets,
        )
        self.assertIn(
            "/plugins/course_selection/static/selection.css?v=20260921-1",
            registry.stylesheets,
        )
        self.assertIn(
            "/plugins/classroom/static/downloads.js?v=20260921-1",
            registry.scripts,
        )
        self.assertIn(
            "/plugins/course_selection/static/selection_ui.js?v=20260921-1",
            registry.scripts,
        )

        classroom_only = build_plugin_registry({"classroom"})
        self.assertIn(
            "/plugins/classroom/static/classroom.css?v=20260921-1",
            classroom_only.stylesheets,
        )
        self.assertNotIn(
            "/plugins/course_selection/static/selection.css?v=20260921-1",
            classroom_only.stylesheets,
        )
        self.assertNotIn(
            "/plugins/course_selection/static/selection_ui.js?v=20260921-1",
            classroom_only.scripts,
        )

        base = Path("templates/base.html").read_text(encoding="utf-8")
        self.assertIn("plugin_registry.stylesheets", base)
        self.assertIn("plugin_registry.scripts", base)

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
        self.assertEqual(
            mounts["core_ui"].path,
            "/plugins/core_ui/static",
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
