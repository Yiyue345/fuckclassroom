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

class _InteractiveLoginAssistant(CourseSelectionAssistant):
    def __init__(self, config: AppConfig) -> None:
        super().__init__(config, credential_store=_SavedCredentialStore())
        self.auto_login_calls = 0
        self.reused_saved_session: bool | None = None

    def _login_with_saved_credentials(
        self,
        credentials: SelectionCredentials,
        *,
        timeout_ms: int,
        progress=None,
    ) -> None:
        self.auto_login_calls += 1

    def _login_interactively(
        self,
        progress=None,
        timeout_ms: int = 600_000,
        credentials: SelectionCredentials | None = None,
        *,
        reuse_saved_session: bool = True,
    ) -> str:
        self.interactive_credentials = credentials
        self.reused_saved_session = reuse_saved_session
        return "/selection"


class _FakeVerificationPage:
    url = "http://cas.guet.edu.cn/authserver/login"

    def wait_for_timeout(self, _timeout_ms: int) -> None:
        pass

    def evaluate(self, _script: str) -> dict[str, object]:
        return {
            "request_ok": True,
            "status": 405,
            "response_url": self.url,
            "page_url": self.url,
            "title": "统一身份认证",
            "body_preview": "",
        }



class CourseSelectionLoginTests(unittest.TestCase):
    def test_expired_session_uses_saved_credentials_once(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = _AutoReloginAssistant(AppConfig(data_dir=Path(directory)))

            status = assistant.verify_saved_session()

            self.assertTrue(status.is_valid)
            self.assertEqual(assistant.login_calls, 1)
            self.assertEqual(assistant.verify_calls, 2)
            self.assertEqual(assistant.assert_credentials.username, "student-id")

    def test_explicit_login_skips_headless_attempt_and_uses_clean_context(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = _InteractiveLoginAssistant(AppConfig(data_dir=Path(directory)))

            result = assistant.login_for_web(force_interactive=True)

            self.assertEqual(result, "/selection")
            self.assertEqual(assistant.auto_login_calls, 0)
            self.assertEqual(assistant.interactive_credentials.username, "student-id")
            self.assertFalse(assistant.reused_saved_session)

    def test_cas_405_is_reported_as_expired_session(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = CourseSelectionAssistant(AppConfig(data_dir=Path(directory)))

            check = assistant._verify_opened_session(object(), _FakeVerificationPage())

            self.assertFalse(check["is_valid"])
            self.assertIn("登录页", check["message"])
            self.assertNotIn("教务访问返回 405", check["message"])


if __name__ == "__main__":
    unittest.main()
