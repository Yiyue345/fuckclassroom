from __future__ import annotations

import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from fuckclassroom.plugins import build_plugin_registry


class SidebarNavigationTests(unittest.TestCase):
    def test_navigation_is_registered_by_plugins(self) -> None:
        registry = build_plugin_registry({"classroom", "course_selection", "processing", "transcription", "ocr", "ai_summary", "attendance"})
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
                "/about",
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
        registry = build_plugin_registry({"classroom", "course_selection", "processing", "transcription", "ocr", "ai_summary", "attendance"})
        panel_keys = [panel.key for panel in registry.settings_panels]

        self.assertEqual(
            panel_keys,
            [
                "appearance",
                "courses",
                "processing",
                "auto-selection",
                "selection-network",
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
        environment.get_template("core/about.html")
        for panel in registry.settings_panels:
            environment.get_template(panel.template)

        self.assertIn('{% set active_system = "common" %}', settings)
        self.assertIn("settings_panels", settings)
        self.assertNotIn("plugin_registry.settings_panels", settings)

        panels = {panel.key: panel for panel in registry.settings_panels}
        self.assertIsNotNone(panels["courses"].context_factory)
        self.assertIsNotNone(panels["selection-network"].context_factory)
        self.assertIsNotNone(panels["network"].context_factory)
        self.assertIsNotNone(panels["storage"].context_factory)
        self.assertIn("plugin_registry.system_label(active_system)", base)
        self.assertEqual(registry.system_label("selection"), "本科选课")

    def test_account_panels_are_registered_by_plugins(self) -> None:
        registry = build_plugin_registry({"classroom", "course_selection", "processing", "transcription", "ocr", "ai_summary", "attendance"})
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

    def test_plugin_manager_matches_split_panel_design(self) -> None:
        template = Path(
            "fuckclassroom/plugins/core_ui/templates/core/plugin_manager.html"
        ).read_text(encoding="utf-8")
        styles = Path(
            "fuckclassroom/plugins/core_ui/static/core_ui.css"
        ).read_text(encoding="utf-8")
        base = Path("templates/base.html").read_text(encoding="utf-8")

        self.assertIn('class="plugin-toolbar surface"', template)
        self.assertIn('id="plugin-search-input"', template)
        self.assertIn('data-plugin-filter="all"', template)
        self.assertIn('data-plugin-filter="enabled"', template)
        self.assertIn('data-plugin-filter="attention"', template)
        self.assertIn('data-plugin-filter="external"', template)
        self.assertIn('class="plugin-manager-layout"', template)
        self.assertIn('class="plugin-list-panel surface"', template)
        self.assertIn('class="plugin-detail-panel surface"', template)
        self.assertIn("data-plugin-row", template)
        self.assertIn("data-plugin-detail", template)
        self.assertIn("plugin-dependency-box", template)
        self.assertIn("plugin-manage-row", template)
        self.assertIn("查看插件技术信息", template)
        self.assertIn('id="plugin-zip-button"', template)
        self.assertIn('onchange="this.form.requestSubmit()"', template)
        self.assertEqual(
            template.count('onchange="this.form.requestSubmit()"'),
            2,
        )
        self.assertIn("applyFilters()", template)
        self.assertIn("selectPlugin(", template)
        self.assertIn("fetch('/api/plugins/runtime-status'", template)
        self.assertIn(".plugin-manager-layout {", styles)
        self.assertIn(
            "grid-template-columns: minmax(0, 1.39fr) minmax(360px, 1fr)",
            styles,
        )
        self.assertIn(".plugin-list-panel { height: 714px;", styles)
        self.assertIn(".plugin-row.is-selected {", styles)
        self.assertIn(".plugin-detail-panel { position: sticky;", styles)
        self.assertIn(".plugin-add-button {", styles)
        self.assertIn("plugin-operation-status", template)
        self.assertIn("data-operation-loading", template)
        self.assertIn("data-operation-done", template)
        self.assertIn("showOperationStatus('loading')", template)
        self.assertIn("showOperationStatus('done')", template)
        self.assertIn("let activeOperationCount = 0", template)
        self.assertIn("function beginOperation()", template)
        self.assertIn("function finishOperation(success = true)", template)
        self.assertIn("pendingToggleOperations = new Map()", template)
        self.assertIn("pendingToggleOperations.set(id, requestedEnabled)", template)
        self.assertIn("pendingToggleOperations.delete(id)", template)
        self.assertIn('data-plugin-error', template)
        self.assertIn("function showPluginError(id, message)", template)
        self.assertIn("errorElement.textContent = text", template)
        self.assertIn("badge.textContent = '加载失败'", template)
        self.assertNotIn("failedBadge.textContent = '修改失败'", template)
        self.assertIn(
            "data-plugin-enabled=\"{{ 'true' if plugin.current_enabled else 'false' }}\"",
            template,
        )
        self.assertIn("data-plugin-static-needs-action", template)
        self.assertIn(
            "row.dataset.pluginEnabled = currentEnabled ? 'true' : 'false'",
            template,
        )
        self.assertIn(
            "row.dataset.pluginNeedsAction = (staticNeedsAction || pending || Boolean(errorMessage)) ? 'true' : 'false'",
            template,
        )
        self.assertIn(
            "const selectedRow = visibleRows.find((row) => row.dataset.pluginId === selectedId)",
            template,
        )
        self.assertIn(
            "selectPlugin(selectedRow ? selectedId : visibleRows[0].dataset.pluginId)",
            template,
        )
        self.assertIn(".plugin-row-error {", styles)
        self.assertIn("if (activeOperationCount > 0)", template)
        self.assertIn("window.FuckClassroomPage?.current?.()", template)
        self.assertIn("function pageFetch(input, init = {})", template)
        self.assertIn("if (disposed()) return;", template)
        self.assertIn("error?.name === 'AbortError'", template)
        self.assertIn("window.FuckClassroomShell?.navigate", template)
        self.assertNotIn("window.location.href = '/plugins?notice='", template)
        self.assertIn(".plugin-operation-done {", styles)
        self.assertNotIn("本机路径", template)
        self.assertNotIn("外部插件包格式", template)
        self.assertEqual(
            template.count("{% block "),
            template.count("{% endblock %}"),
        )
        self.assertIn("/static/app.css?v=20260927-1", base)

    def test_plugin_ui_assets_are_registered_by_enabled_plugins(self) -> None:
        registry = build_plugin_registry({"classroom", "course_selection", "processing", "transcription", "ocr", "ai_summary", "attendance"})

        self.assertIn(
            "/plugins/core_ui/static/core_ui.css?v=20260927-1",
            registry.stylesheets,
        )
        self.assertIn(
            "/plugins/classroom/static/classroom.css?v=20260922-1",
            registry.stylesheets,
        )
        self.assertIn(
            "/plugins/attendance/static/attendance.css?v=20260922-1",
            registry.stylesheets,
        )
        self.assertIn(
            "/plugins/processing/static/processing.css?v=20260922-1",
            registry.stylesheets,
        )
        self.assertIn(
            "/plugins/course_selection/static/selection.css?v=20260922-1",
            registry.stylesheets,
        )
        self.assertIn(
            "/plugins/classroom/static/downloads.js?v=20260921-1",
            registry.page_assets("downloads", "script") + registry.page_assets("selection_info", "script"),
        )
        self.assertIn(
            "/plugins/course_selection/static/selection_ui.js?v=20260927-2",
            registry.page_assets("downloads", "script") + registry.page_assets("selection_info", "script"),
        )

        classroom_only = build_plugin_registry({"classroom"})
        self.assertIn(
            "/plugins/classroom/static/classroom.css?v=20260922-1",
            classroom_only.stylesheets,
        )
        self.assertNotIn(
            "/plugins/course_selection/static/selection.css?v=20260922-1",
            classroom_only.stylesheets,
        )
        self.assertNotIn(
            "/plugins/course_selection/static/selection_ui.js?v=20260927-2",
            classroom_only.scripts,
        )

        base = Path("templates/base.html").read_text(encoding="utf-8")
        self.assertIn('plugin_registry.page_assets(ui_page, "style")', base)
        self.assertIn('plugin_registry.page_assets(ui_page, "script")', base)

    def test_plugin_static_mounts_are_isolated(self) -> None:
        registry = build_plugin_registry({"classroom", "course_selection", "processing", "transcription", "ocr", "ai_summary", "attendance"})
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

    def test_shell_uses_partial_navigation_and_preserves_sidebar(self) -> None:
        shell = Path("static/shell.js").read_text(encoding="utf-8")
        app = Path("static/app.js").read_text(encoding="utf-8")
        styles = Path("static/app.css").read_text(encoding="utf-8")
        base = Path("templates/base.html").read_text(encoding="utf-8")

        self.assertIn("async function navigate(url, options = {})", shell)
        self.assertIn("fetch(destination.href", shell)
        self.assertIn("history.pushState", shell)
        self.assertIn("window.addEventListener('popstate'", shell)
        self.assertIn("sidebarNav.scrollTop = sidebarScroll", shell)
        self.assertIn("document.startViewTransition", shell)
        self.assertIn("academic:page-before-swap", shell)
        self.assertIn("academic:page-load", shell)
        self.assertIn("function beginIntervalCapture()", shell)
        self.assertIn("clearPageIntervals()", shell)
        self.assertIn("function createPageRuntime()", shell)
        self.assertIn("window.FuckClassroomPage = Object.freeze", shell)
        self.assertIn("currentPageRuntime?.dispose()", shell)
        self.assertIn("fuckClassroomHistoryIndex", shell)
        self.assertIn("function allowNavigation()", shell)
        self.assertIn("restoringHistory = true", shell)
        self.assertIn("history.go(historyIndex - targetHistoryIndex)", shell)
        self.assertIn("function replaceUrl(url, state = {})", shell)
        self.assertIn("displayedUrl = window.location.href", shell)
        self.assertIn("window.FuckClassroomShell = Object.freeze({ navigate, initPage, replaceUrl })", shell)
        self.assertIn("window.FuckClassroomUI?.initPage?.(pageRoot)", shell)
        self.assertIn("function initPage(rootNode = document)", app)
        self.assertIn("data-inline-task", app)
        self.assertIn("view-transition-name: page-content", styles)
        self.assertIn("body.pjax-loading", styles)
        self.assertIn("/static/app.css?v=20260927-1", base)
        self.assertIn("/static/app.js?v=20260927-2", base)
        self.assertIn("/static/shell.js?v=20260928-1", base)

    def test_selection_async_flows_share_pjax_page_lifecycle(self) -> None:
        selection_ui = Path(
            "fuckclassroom/plugins/course_selection/static/selection_ui.js"
        ).read_text(encoding="utf-8")
        catalog = Path(
            "fuckclassroom/plugins/course_selection/static/selection_catalog.js"
        ).read_text(encoding="utf-8")
        plugin = Path(
            "fuckclassroom/plugins/course_selection/plugin.py"
        ).read_text(encoding="utf-8")

        for script in (selection_ui, catalog):
            self.assertIn("window.FuckClassroomPage?.current?.()", script)
            self.assertIn("signal: pageSignal", script)
            self.assertIn("function disposed()", script)
            self.assertIn('error?.name === "AbortError"', script)

        self.assertIn("while (!disposed())", selection_ui)
        self.assertIn("while (!disposed())", catalog)
        self.assertIn("window.FuckClassroomShell?.replaceUrl", catalog)
        self.assertIn("window.FuckClassroomShell.replaceUrl(visibleUrl", catalog)
        self.assertIn("selection_ui.js?v=20260927-2", plugin)
        self.assertIn("selection_catalog.js?v=20260928-1", plugin)

    def test_course_media_async_flows_share_pjax_page_lifecycle(self) -> None:
        media = Path(
            "fuckclassroom/plugins/classroom/static/course_media.js"
        ).read_text(encoding="utf-8")
        plugin = Path(
            "fuckclassroom/plugins/classroom/plugin.py"
        ).read_text(encoding="utf-8")

        self.assertIn("window.FuckClassroomPage?.current?.()", media)
        self.assertIn("function pageFetch(input, init = {})", media)
        self.assertIn("signal: pageSignal", media)
        self.assertIn("pageRuntime?.onDispose?.(cleanupPage)", media)
        self.assertIn("pptLoadToken += 1", media)
        self.assertIn("videoLoadToken += 1", media)
        self.assertIn("if (disposed() || token !== pptLoadToken)", media)
        self.assertIn("if (disposed() || token !== videoLoadToken)", media)
        self.assertNotIn("const pageController = new AbortController()", media)
        self.assertNotIn("await fetch(", media)
        self.assertIn("course_media.js?v=20260928-1", plugin)

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
