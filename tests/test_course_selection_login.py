from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fuckclassroom.config import AppConfig
from fuckclassroom.course_selection import CourseSelectionAssistant, WebVpnSessionStatus
from fuckclassroom.auth.credentials import SelectionCredentials


class _SavedCredentialStore:
    def load(self) -> SelectionCredentials:
        return SelectionCredentials("student-id", "saved-password")

    def public_status(self) -> dict[str, object]:
        return {"is_saved": True, "username": "student-id", "message": "saved"}


class _AutoReloginAssistant(CourseSelectionAssistant):
    def __init__(self, config: AppConfig) -> None:
        super().__init__(config, credential_store=_SavedCredentialStore())
        self.login_calls = 0
        self.verify_calls = 0

    def _verify_saved_session_once(self, timeout_ms: int) -> WebVpnSessionStatus:
        self.verify_calls += 1
        if self.verify_calls == 1:
            return WebVpnSessionStatus(True, "expired", is_valid=False)
        return WebVpnSessionStatus(True, "valid", is_valid=True)

    def _login_with_saved_credentials(
        self,
        credentials: SelectionCredentials,
        *,
        timeout_ms: int,
    ) -> None:
        self.login_calls += 1
        self.assert_credentials = credentials


class CourseSelectionLoginTests(unittest.TestCase):
    def test_expired_session_uses_saved_credentials_once(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = _AutoReloginAssistant(AppConfig(data_dir=Path(directory)))

            status = assistant.verify_saved_session()

            self.assertTrue(status.is_valid)
            self.assertEqual(assistant.login_calls, 1)
            self.assertEqual(assistant.verify_calls, 2)
            self.assertEqual(assistant.assert_credentials.username, "student-id")


if __name__ == "__main__":
    unittest.main()
