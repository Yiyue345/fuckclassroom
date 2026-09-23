import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from fuckclassroom.core.config import AppConfig
from fuckclassroom.course_selection.models import WebVpnSessionStatus
from fuckclassroom.web import create_app


class LabSelectionTests(unittest.TestCase):
    def make_app(self, plugins):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return create_app(AppConfig(data_dir=Path(temp.name)), enabled_plugins=plugins)

    def test_can_enable_without_regular_selection(self):
        app = self.make_app({"lab_selection"})
        services = app.state.plugin_runtime.services
        self.assertFalse(services.has("course_selection"))
        self.assertFalse(services.has("auto_selection"))
        self.assertFalse(services.has("course_selection_process_host"))
        session = services.get("academic_session")
        with patch.object(session, "get_session_status", return_value=WebVpnSessionStatus(False, "尚未登录")):
            with TestClient(app) as client:
                response = client.get("/lab-selection")
                self.assertEqual(response.status_code, 200)
                self.assertIn("查询课程", response.text)
                self.assertEqual(client.get("/selection/info").status_code, 404)

    def test_both_plugins_share_the_same_session_and_proxy(self):
        app = self.make_app({"course_selection", "lab_selection"})
        services = app.state.plugin_runtime.services
        lab = services.get("lab_selection")
        self.assertIs(lab.session, services.get("course_selection_local"))
        self.assertIs(lab.session.hy2_proxy, services.get("hy2_proxy"))
        with patch.object(lab.session, "login_for_web", return_value="/selection") as login:
            self.assertEqual(lab.login(None), "/lab-selection")
            login.assert_called_once_with(progress=None, force_interactive=True)

    def test_disabled_plugin_does_not_create_shared_session(self):
        app = self.make_app(set())
        self.assertFalse(app.state.plugin_runtime.services.has("academic_session"))
        self.assertNotIn("/lab-selection", {getattr(route, "path", "") for route in app.routes})

    def test_lab_only_session_clear_uses_shared_session(self):
        import asyncio

        app = self.make_app({"lab_selection"})
        runtime = app.state.plugin_runtime
        session = runtime.services.get("academic_session")
        with patch.object(session, "clear_session") as clear:
            asyncio.run(runtime.clear_plugin_sessions())
            clear.assert_called_once_with()

    def test_waitlist_routes_create_list_and_cancel_without_enrollment(self):
        app = self.make_app({"lab_selection"})
        lab = app.state.plugin_runtime.services.get("lab_selection")
        client = TestClient(app)  # No lifespan: scheduler is intentionally stopped.
        with patch.object(lab, "target_state", return_value={"status": "waiting", "message": "已满", "account_id": "test"}), patch.object(lab, "select") as select:
            response = client.post("/api/lab-selection/waitlist", json={"task_id": "t", "group_id": "g"})
            self.assertEqual(response.status_code, 200)
            job_id = response.json()["id"]
            jobs = client.get("/api/lab-selection/waitlist").json()
            self.assertEqual(jobs[0]["status"], "waiting")
            self.assertNotIn("account_id", jobs[0])
            self.assertEqual(client.post(f"/api/lab-selection/waitlist/{job_id}/cancel").status_code, 200)
            self.assertEqual(client.get("/api/lab-selection/waitlist").json()[0]["status"], "cancelled")
            select.assert_not_called()

    def test_waitlist_rejects_bad_interval_and_foreign_origin(self):
        client = TestClient(self.make_app({"lab_selection"}))
        body = {"task_id": "t", "group_id": "g", "interval": 1}
        self.assertEqual(client.post("/api/lab-selection/waitlist", json=body).status_code, 422)
        body["interval"] = 30
        self.assertEqual(client.post("/api/lab-selection/waitlist", json=body, headers={"Origin": "https://elsewhere.invalid"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
