from __future__ import annotations

import unittest
from pathlib import Path


class UiModularizationTests(unittest.TestCase):
    def test_feature_pages_are_owned_by_plugin_template_directories(self) -> None:
        root = Path("templates")
        forbidden = {
            "accounts.html",
            "settings.html",
            "tasks.html",
            "task_status.html",
            "login.html",
            "courses.html",
            "course_detail.html",
            "downloads.html",
            "lesson_outputs.html",
            "selection_catalog.html",
            "selection_info.html",
            "selection_auto.html",
            "selection_captures.html",
            "selection_capture_detail.html",
        }
        root_templates = {path.name for path in root.glob("*.html")}
        self.assertFalse(forbidden.intersection(root_templates))
        self.assertEqual(root_templates, {"base.html"})

        expected = (
            Path("fuckclassroom/plugins/core_ui/templates/core/accounts.html"),
            Path("fuckclassroom/plugins/core_ui/templates/core/settings.html"),
            Path("fuckclassroom/plugins/core_ui/templates/core/tasks.html"),
            Path("fuckclassroom/plugins/core_ui/templates/core/task_status.html"),
            Path("fuckclassroom/plugins/core_ui/templates/core/plugin_manager.html"),
            Path("fuckclassroom/plugins/core_ui/templates/core/about.html"),
            Path("fuckclassroom/plugins/classroom/templates/classroom/login.html"),
            Path("fuckclassroom/plugins/classroom/templates/classroom/courses.html"),
            Path("fuckclassroom/plugins/classroom/templates/classroom/course_detail.html"),
            Path("fuckclassroom/plugins/classroom/templates/classroom/downloads.html"),
            Path("fuckclassroom/plugins/processing/templates/processing/lesson_outputs.html"),
            Path("fuckclassroom/plugins/course_selection/templates/selection/catalog.html"),
            Path("fuckclassroom/plugins/course_selection/templates/selection/info.html"),
            Path("fuckclassroom/plugins/course_selection/templates/selection/auto.html"),
            Path("fuckclassroom/plugins/course_selection/templates/selection/captures.html"),
            Path("fuckclassroom/plugins/course_selection/templates/selection/capture_detail.html"),
        )
        for path in expected:
            with self.subTest(path=path):
                self.assertTrue(path.is_file())

    def test_accounts_shell_has_no_feature_specific_panels(self) -> None:
        shell = Path(
            "fuckclassroom/plugins/core_ui/templates/core/accounts.html"
        ).read_text(encoding="utf-8")
        core_routes = Path(
            "fuckclassroom/plugins/core_ui/routes.py"
        ).read_text(encoding="utf-8")

        self.assertIn("account_panels", shell)
        self.assertNotIn("classroom-session", shell)
        self.assertNotIn("selection-session", shell)
        self.assertNotIn("ketangpai-session", shell)
        self.assertNotIn("ketangpai_client", core_routes)
        self.assertNotIn("ketangpai_sms_sessions", core_routes)

        for path in (
            Path("fuckclassroom/plugins/classroom/templates/classroom/account_panel.html"),
            Path("fuckclassroom/plugins/course_selection/templates/selection/account_panel.html"),
            Path("fuckclassroom/plugins/attendance/templates/attendance/account_panel.html"),
        ):
            with self.subTest(path=path):
                self.assertTrue(path.is_file())

    def test_settings_shell_uses_plugin_panel_contexts(self) -> None:
        shell = Path(
            "fuckclassroom/plugins/core_ui/templates/core/settings.html"
        ).read_text(encoding="utf-8")
        core_routes = Path(
            "fuckclassroom/plugins/core_ui/routes.py"
        ).read_text(encoding="utf-8")
        classroom_panel = Path(
            "fuckclassroom/plugins/classroom/templates/classroom_settings_courses.html"
        ).read_text(encoding="utf-8")

        self.assertIn("settings_panels", shell)
        self.assertIn("panel_context", shell)
        self.assertNotIn("plugin_registry.settings_panels", shell)
        self.assertNotIn("semester_sync", core_routes)
        self.assertNotIn('"terms": terms', core_routes)
        self.assertIn("panel_context.terms", classroom_panel)

    def test_feature_javascript_is_not_kept_in_core_app_bundle(self) -> None:
        core = Path("static/app.js").read_text(encoding="utf-8")
        classroom = Path(
            "fuckclassroom/plugins/classroom/static/downloads.js"
        ).read_text(encoding="utf-8")
        selection = Path(
            "fuckclassroom/plugins/course_selection/static/selection_ui.js"
        ).read_text(encoding="utf-8")

        self.assertIn("window.FuckClassroomUI", core)
        self.assertNotIn("[data-download-library]", core)
        self.assertNotIn("[data-selection-action]", core)
        self.assertIn("[data-download-library]", classroom)
        self.assertIn("[data-selection-action]", selection)
        self.assertIn("selection-count-refresh-enabled", selection)

    def test_feature_styles_are_not_kept_in_core_stylesheet(self) -> None:
        core = Path("static/app.css").read_text(encoding="utf-8")
        classroom = Path(
            "fuckclassroom/plugins/classroom/static/classroom.css"
        ).read_text(encoding="utf-8")
        processing = Path(
            "fuckclassroom/plugins/processing/static/processing.css"
        ).read_text(encoding="utf-8")
        selection = Path(
            "fuckclassroom/plugins/course_selection/static/selection.css"
        ).read_text(encoding="utf-8")
        core_ui = Path(
            "fuckclassroom/plugins/core_ui/static/core_ui.css"
        ).read_text(encoding="utf-8")
        attendance = Path(
            "fuckclassroom/plugins/attendance/static/attendance.css"
        ).read_text(encoding="utf-8")

        self.assertNotIn(".course-summary", core)
        self.assertNotIn(".output-actions", core)
        self.assertNotIn(".catalog-filters", core)
        self.assertNotIn(".settings-layout", core)
        self.assertNotIn(".plugin-manager-layout", core)
        self.assertNotIn(".ketangpai-overview", core)
        self.assertNotIn(".task-stack", core)
        self.assertIn(".course-summary", classroom)
        self.assertIn(".output-actions", processing)
        self.assertIn(".catalog-filters", selection)
        self.assertIn(".settings-layout", core_ui)
        self.assertIn(".plugin-manager-layout", core_ui)
        self.assertNotIn(".plugin-manager-grid", core_ui)
        self.assertIn(".task-stack", core_ui)
        self.assertNotIn(".ketangpai-overview", core_ui)
        self.assertIn(".ketangpai-overview", attendance)

    def test_frontend_regressions_for_open_ui_issues(self) -> None:
        network = Path(
            "fuckclassroom/plugins/core_ui/templates/core_settings_network.html"
        ).read_text(encoding="utf-8")
        classroom = Path(
            "fuckclassroom/plugins/classroom/templates/classroom/login.html"
        ).read_text(encoding="utf-8")
        task_script = Path(
            "fuckclassroom/plugins/core_ui/static/task_status.js"
        ).read_text(encoding="utf-8")
        app_script = Path("static/app.js").read_text(encoding="utf-8")
        base = Path("templates/base.html").read_text(encoding="utf-8")
        task_template = Path(
            "fuckclassroom/plugins/core_ui/templates/core/task_status.html"
        ).read_text(encoding="utf-8")

        # #18: a damaged credential file is not "saved", but must remain clearable.
        self.assertIn(
            '{% if panel_context.credentials.can_clear %}\n'
            '      <button class="btn danger" type="submit" form="credential-clear-form"',
            network,
        )
        self.assertIn(
            "panel_context.credentials.is_saved",
            network,
        )

        # #24: recent tasks use the same cancelled/cancelling semantics as task center.
        self.assertIn('task.status == "cancelled"', classroom)
        self.assertIn("已取消", classroom)
        self.assertIn("取消中", classroom)
        self.assertIn("进行中 {{ task.progress }}%", classroom)

        # #21: transient HTTP failures retry; missing/expired tasks stop with feedback.
        self.assertIn("response.status === 404 || response.status === 410", task_script)
        self.assertIn("正在重试", task_script)
        self.assertIn("schedule(refreshTask, 1500)", task_script)
        self.assertIn("task_status.js?v=20260927-1", task_template)

        # #46: task detail polling is scoped to the current PJAX page.
        self.assertIn("window.FuckClassroomPage?.current?.()", task_script)
        self.assertIn("signal: pageSignal", task_script)
        self.assertIn("schedule(refreshTask, 1500)", task_script)
        self.assertNotIn("window.setTimeout(refreshTask, 1500)", task_script)

        # #47: inline task polling aborts when its originating page is replaced.
        self.assertIn("const pageRuntime = window.FuckClassroomPage?.current?.()", app_script)
        self.assertIn("while (!pageSignal || !pageSignal.aborted)", app_script)
        self.assertIn("signal: pageSignal", app_script)
        self.assertIn('error?.name === "AbortError"', app_script)
        self.assertIn("window.FuckClassroomShell?.navigate", app_script)

        # #48: plugin manager async operations are scoped to the current PJAX generation.
        plugin_manager = Path(
            "fuckclassroom/plugins/core_ui/templates/core/plugin_manager.html"
        ).read_text(encoding="utf-8")
        self.assertIn("const pageRuntime = window.FuckClassroomPage?.current?.()", plugin_manager)
        self.assertIn("const pageSignal = pageRuntime?.signal", plugin_manager)
        self.assertIn("return fetch(url, { ...options, signal: pageSignal })", plugin_manager)
        self.assertIn("if (!pageIsActive()) return", plugin_manager)
        self.assertIn("if (isPageAbort(error)) return", plugin_manager)
        self.assertIn("pageRuntime.setTimeout(callback, delay)", plugin_manager)
        self.assertIn("window.FuckClassroomShell?.navigate", plugin_manager)
        self.assertNotIn("window.location.href = '/plugins?notice='", plugin_manager)

        # #19: idle pages no longer poll auth every 800 ms forever.
        self.assertNotIn("window.setInterval(pollAuthChallenge, 800)", app_script)
        self.assertIn("return 15000", app_script)
        self.assertIn('academic:auth-poll-wake', app_script)
        self.assertIn("scheduleAuthPoll(0)", app_script)
        self.assertIn("/static/app.js?v=20260927-2", base)

    def test_core_shell_and_task_pages_are_feature_agnostic(self) -> None:
        base = Path("templates/base.html").read_text(encoding="utf-8")
        tasks = Path(
            "fuckclassroom/plugins/core_ui/templates/core/tasks.html"
        ).read_text(encoding="utf-8")
        detail = Path(
            "fuckclassroom/plugins/core_ui/templates/core/task_status.html"
        ).read_text(encoding="utf-8")
        routes = Path(
            "fuckclassroom/plugins/core_ui/routes.py"
        ).read_text(encoding="utf-8")

        self.assertIn('<a class="brand" href="/">', base)
        self.assertNotIn('<a class="brand" href="/classroom">', base)
        self.assertIn("任务中心", tasks)
        for phrase in ("OCR、语音转写", "AI 总结和选课"):
            self.assertNotIn(phrase, tasks)
        self.assertIn('href="/tasks"', detail)
        self.assertNotIn("active_system == 'selection'", detail)
        self.assertNotIn("selection_task = any(", routes)

    def test_task_detail_assets_belong_to_core_ui_plugin(self) -> None:
        template = Path(
            "fuckclassroom/plugins/core_ui/templates/core/task_status.html"
        ).read_text(encoding="utf-8")

        self.assertIn("/plugins/core_ui/static/task_status.css", template)
        self.assertIn("/plugins/core_ui/static/task_status.js", template)
        self.assertIn("data-task-page", template)
        self.assertTrue(
            Path("fuckclassroom/plugins/core_ui/static/task_status.css").is_file()
        )
        self.assertTrue(
            Path("fuckclassroom/plugins/core_ui/static/task_status.js").is_file()
        )
        self.assertFalse(Path("static/task_status.css").exists())


    def test_core_ui_settings_are_feature_agnostic(self) -> None:
        routes = Path("fuckclassroom/plugins/core_ui/routes.py").read_text(encoding="utf-8")
        network = Path(
            "fuckclassroom/plugins/core_ui/templates/core_settings_network.html"
        ).read_text(encoding="utf-8")
        selection_network = Path(
            "fuckclassroom/plugins/course_selection/templates/selection_settings_network.html"
        ).read_text(encoding="utf-8")
        selection_hooks = Path(
            "fuckclassroom/plugins/course_selection/settings_hooks.py"
        ).read_text(encoding="utf-8")

        for feature_lookup in (
            'services.maybe("hy2_proxy")',
            'services.maybe("course_selection")',
            'services.maybe("auto_selection")',
        ):
            self.assertNotIn(feature_lookup, routes)
        self.assertIn("context.notify_settings_saved", routes)
        self.assertIn("context.clear_plugin_sessions", routes)
        self.assertNotIn("Hy2", network)
        self.assertNotIn("本科教务", network)
        self.assertNotIn("selection_username", network)
        self.assertNotIn("selection_password", network)
        self.assertIn("account_username", network)
        self.assertIn("account_password", network)
        self.assertIn("Hy2", selection_network)
        self.assertIn("hy2_proxy", selection_hooks)
        self.assertIn("auto_selection.configure_intervals", selection_hooks)

    def test_task_behavior_is_declared_by_task_owner(self) -> None:
        core_routes = Path(
            "fuckclassroom/plugins/core_ui/routes.py"
        ).read_text(encoding="utf-8")
        task_template = Path(
            "fuckclassroom/plugins/core_ui/templates/core/task_status.html"
        ).read_text(encoding="utf-8")
        task_script = Path(
            "fuckclassroom/plugins/core_ui/static/task_status.js"
        ).read_text(encoding="utf-8")
        processing_routes = Path(
            "fuckclassroom/plugins/processing/routes.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("auto_open_outputs_after_extract", core_routes)
        self.assertNotIn('"提取文字"', core_routes)
        self.assertNotIn("data-auto-open", task_template)
        self.assertIn("task.auto_open_result", task_script)
        self.assertIn(
            "auto_open_result=context.config.auto_open_outputs_after_extract",
            processing_routes,
        )


if __name__ == "__main__":
    unittest.main()
