from __future__ import annotations

import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


class SidebarNavigationTests(unittest.TestCase):
    def test_all_product_areas_are_visible_without_a_workspace_menu(self) -> None:
        template = Path("templates/base.html").read_text(encoding="utf-8")

        self.assertNotIn("data-workspace-switcher", template)
        for label in ("课程录播导航", "本科选课导航", "通用导航"):
            self.assertIn(f'aria-label="{label}"', template)
        for href in (
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
            "/settings",
        ):
            self.assertIn(f'href="{href}"', template)

    def test_settings_use_the_common_navigation_context(self) -> None:
        settings = Path("templates/settings.html").read_text(encoding="utf-8")
        base = Path("templates/base.html").read_text(encoding="utf-8")
        environment = Environment(loader=FileSystemLoader("templates"))

        environment.get_template("base.html")
        environment.get_template("settings.html")
        self.assertIn('{% set active_system = "common" %}', settings)
        self.assertIn('active_system == "common"', base)
        self.assertIn("通用", base)

    def test_session_footer_title_and_theme_button_share_the_same_track(self) -> None:
        styles = Path("static/app.css").read_text(encoding="utf-8")

        self.assertRegex(styles, r"\.sidebar-footer-head \{[^}]*height: 32px;")
        self.assertIn(
            ".sidebar-footer-head > span { display: flex; height: 32px; align-items: center; }",
            styles,
        )
        self.assertIn(
            ".sidebar-theme-toggle.btn.icon-only { width: 32px; height: 32px; min-height: 32px; flex: 0 0 32px; }",
            styles,
        )


if __name__ == "__main__":
    unittest.main()
