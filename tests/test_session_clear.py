from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fuckclassroom.config import AppConfig
from fuckclassroom.course_selection import CourseSelectionAssistant
from fuckclassroom.web import create_app


class SessionClearTests(unittest.TestCase):
    def test_selection_session_clear_removes_login_state_and_token_only(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = AppConfig(data_dir=Path(directory))
            assistant = CourseSelectionAssistant(config)
            preserved = assistant.selection_info_path
            session_files = (
                assistant.storage_state_path,
                assistant.session_meta_path,
                assistant.session_check_path,
            )
            for path in (*session_files, preserved):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            assistant._selection_token = "selection-token"
            assistant._selection_token_expires_at = 123.0
            assistant._selection_token_mode = "direct"

            assistant.clear_session()

            self.assertTrue(all(not path.exists() for path in session_files))
            self.assertTrue(preserved.exists())
            self.assertEqual(assistant._selection_token, "")
            self.assertEqual(assistant._selection_token_expires_at, 0.0)
            self.assertEqual(assistant._selection_token_mode, "")

    def test_settings_action_clears_both_sessions_but_preserves_credentials(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = AppConfig(data_dir=Path(directory))
            classroom_files = (
                config.storage_state_path,
                config.session_meta_path,
                config.login_status_path,
            )
            selection_files = tuple(
                config.data_dir / "webvpn" / name
                for name in ("storage_state.json", "session_meta.json", "session_check.json")
            )
            credential_path = config.data_dir / "account" / "credentials.json"
            for path in (*classroom_files, *selection_files, credential_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("preserved" if path == credential_path else "{}", encoding="utf-8")
            app = create_app(config)
            route = next(
                item
                for item in app.routes
                if getattr(item, "path", "") == "/settings/login-state/clear"
            )

            response = route.endpoint()

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/settings?login_state_cleared=1")
            self.assertTrue(all(not path.exists() for path in (*classroom_files, *selection_files)))
            self.assertEqual(credential_path.read_text(encoding="utf-8"), "preserved")


if __name__ == "__main__":
    unittest.main()
