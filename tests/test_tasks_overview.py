from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from fuckclassroom.core.config import AppConfig
from fuckclassroom.core.tasks import TaskManager
from fuckclassroom.web import create_app


class TaskOverviewTests(unittest.TestCase):
    def test_live_overview_api_updates_progress_and_status_without_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                AppConfig(data_dir=Path(directory)), enabled_plugins=("core_ui",)
            )
            manager: TaskManager = app.state.plugin_runtime.services.get("task_manager")
            with TestClient(app) as client:
                empty = client.get("/api/tasks")
                self.assertEqual(empty.status_code, 200)
                self.assertEqual(empty.headers["cache-control"], "no-store")
                self.assertEqual(empty.json(), {"tasks": []})

                task = manager.create_external("音频转写")
                first = client.get("/api/tasks").json()["tasks"]
                self.assertEqual(len(first), 1)
                self.assertEqual(first[0]["id"], task.id)
                self.assertEqual(first[0]["progress"], 0)
                self.assertEqual(first[0]["status"], "running")
                self.assertEqual(first[0]["logs_count"], 1)
                self.assertNotIn("logs", first[0])

                manager.update_external(task.id, progress=43, message="转写中 43%")
                manager.log_external(task.id, "识别第 2 段")
                second = client.get("/api/tasks").json()["tasks"]
                self.assertEqual(second[0]["progress"], 43)
                self.assertEqual(second[0]["message"], "转写中 43%")
                self.assertEqual(second[0]["logs_count"], 3)
                self.assertNotIn("logs", second[0])

                manager.complete_external(task.id)
                done = client.get("/api/tasks").json()["tasks"]
                self.assertEqual(done[0]["status"], "succeeded")
                self.assertEqual(done[0]["progress"], 100)

    def test_overview_page_contains_polling_and_safe_back_control(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                AppConfig(data_dir=Path(directory)), enabled_plugins=("core_ui",)
            )
            with TestClient(app) as client:
                page = client.get("/tasks")
                self.assertEqual(page.status_code, 200)
                self.assertIn("data-task-overview", page.text)
                self.assertIn("data-task-list", page.text)
                self.assertIn("data-task-refresh-status", page.text)
                self.assertIn('href="/" data-task-back', page.text)
                self.assertIn("返回上一页", page.text)
                self.assertIn("/plugins/core_ui/static/tasks.js?v=20261022-1", page.text)
                script = client.get("/plugins/core_ui/static/tasks.js")
                self.assertEqual(script.status_code, 200)
                self.assertIn('fetch("/api/tasks"', script.text)
                self.assertIn("history.back()", script.text)
                self.assertIn("renderRow(row, task)", script.text)
                self.assertIn("cache: \"no-store\"", script.text)

    def test_new_task_appears_after_first_view_and_payload_stays_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                AppConfig(data_dir=Path(directory)), enabled_plugins=("core_ui",)
            )
            manager: TaskManager = app.state.plugin_runtime.services.get("task_manager")
            with TestClient(app) as client:
                page = client.get("/tasks")
                self.assertNotIn("后台导出", page.text)
                task = manager.create_external("后台导出")
                manager.log_external(task.id, "started", level="debug")
                next_snapshot = client.get("/api/tasks").json()
                self.assertEqual(next_snapshot["tasks"][0]["title"], "后台导出")
                self.assertEqual(next_snapshot["tasks"][0]["logs_count"], 2)
                self.assertEqual(
                    set(next_snapshot["tasks"][0]),
                    {
                        "id", "title", "status", "progress", "message",
                        "created_at", "updated_at", "logs_count", "cancel_requested",
                    },
                )


if __name__ == "__main__":
    unittest.main()
