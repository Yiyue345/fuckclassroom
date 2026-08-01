from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fuckclassroom.config import AppConfig
from fuckclassroom.course_selection import (
    CourseSelectionApiError,
    CourseSelectionAssistant,
    WebVpnSessionStatus,
)
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


class _PageVerificationAssistant(CourseSelectionAssistant):
    def __init__(self, config: AppConfig, status: WebVpnSessionStatus) -> None:
        super().__init__(config, credential_store=_SavedCredentialStore())
        self.status = status
        self.verify_calls: list[tuple[int, bool]] = []
        self.open_calls = 0

    def verify_saved_session(
        self,
        timeout_ms: int = 15_000,
        *,
        auto_relogin: bool = True,
    ) -> WebVpnSessionStatus:
        self.verify_calls.append((timeout_ms, auto_relogin))
        return self.status

    def _open_saved_session_page(self, *args, **kwargs) -> str:
        self.open_calls += 1
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


class _FakeLdapLocator:
    def __init__(self, page, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def count(self) -> int:
        return int(self.selector in self.page.visible_selectors)

    def wait_for(self, *, state: str, timeout: int) -> None:
        self.page.waited.append((self.selector, state, timeout))

    def is_visible(self) -> bool:
        return self.selector in self.page.visible_selectors

    def fill(self, value: str) -> None:
        self.page.filled[self.selector] = value

    def evaluate(self, _script: str) -> None:
        self.page.submitted = True

    def inner_text(self) -> str:
        return self.page.error_text


class _FakeLdapPage:
    url = "https://bkjwtest.guet.edu.cn/student/ldap/login"

    def __init__(self, *visible_selectors: str, error_text: str = "") -> None:
        self.visible_selectors = set(visible_selectors)
        self.error_text = error_text
        self.waited: list[tuple[str, str, int]] = []
        self.filled: dict[str, str] = {}
        self.submitted = False

    def locator(self, selector: str) -> _FakeLdapLocator:
        return _FakeLdapLocator(self, selector)



class CourseSelectionLoginTests(unittest.TestCase):
    def test_public_ldap_form_uses_saved_credentials(self) -> None:
        page = _FakeLdapPage(
            'input[placeholder="用户名"]',
            'input[placeholder="密码"]',
            'button:has-text("登录")',
        )

        submitted = CourseSelectionAssistant._submit_saved_credentials(
            page,
            SelectionCredentials("student-id", "saved-password"),
        )

        self.assertTrue(submitted)
        self.assertEqual(page.filled['input[placeholder="用户名"]'], "student-id")
        self.assertEqual(page.filled['input[placeholder="密码"]'], "saved-password")
        self.assertTrue(page.submitted)

    def test_public_ldap_interaction_reason_exposes_captcha_and_error(self) -> None:
        captcha_page = _FakeLdapPage('input[placeholder="验证码"]')
        error_page = _FakeLdapPage(".text-danger", error_text="认证服务暂不可用")

        self.assertEqual(
            CourseSelectionAssistant._login_interaction_reason(captcha_page),
            "公网教务登录要求输入图形验证码",
        )
        self.assertEqual(
            CourseSelectionAssistant._login_interaction_reason(error_page),
            "公网教务返回：认证服务暂不可用",
        )


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

    def test_page_verification_automatically_recovers_before_opening(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = _PageVerificationAssistant(
                AppConfig(data_dir=Path(directory)),
                WebVpnSessionStatus(True, "Hy2 代理教务会话有效", is_valid=True),
            )

            home_result = assistant.open_home_for_web()
            selection_result = assistant.open_course_select_for_web()

            self.assertEqual(home_result, "/selection")
            self.assertEqual(selection_result, "/selection")
            self.assertEqual(assistant.verify_calls, [(15_000, True), (15_000, True)])
            self.assertEqual(assistant.open_calls, 1)

    def test_page_verification_returns_error_only_after_recovery_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = _PageVerificationAssistant(
                AppConfig(data_dir=Path(directory)),
                WebVpnSessionStatus(
                    True,
                    "自动登录失败：统一认证要求输入图形验证码",
                    is_valid=False,
                ),
            )

            result = assistant.open_course_select_for_web()

            self.assertIn("select_error=", result)
            self.assertEqual(assistant.verify_calls, [(15_000, True)])
            self.assertEqual(assistant.open_calls, 0)

    def test_api_operation_retries_once_after_session_recovery(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = _PageVerificationAssistant(
                AppConfig(data_dir=Path(directory)),
                WebVpnSessionStatus(True, "Hy2 代理教务会话有效", is_valid=True),
            )
            attempts = 0

            def operation() -> str:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise CourseSelectionApiError("选课 token 已失效，请重新登录")
                return "recovered"

            result = assistant._run_with_session_recovery(operation)

            self.assertEqual(result, "recovered")
            self.assertEqual(attempts, 2)
            self.assertEqual(assistant.verify_calls, [(15_000, True)])

    def test_api_operation_does_not_relogin_for_business_failure(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = _PageVerificationAssistant(
                AppConfig(data_dir=Path(directory)),
                WebVpnSessionStatus(True, "Hy2 代理教务会话有效", is_valid=True),
            )

            def operation() -> str:
                raise CourseSelectionApiError("课程容量已满")

            with self.assertRaisesRegex(CourseSelectionApiError, "课程容量已满"):
                assistant._run_with_session_recovery(operation)

            self.assertEqual(assistant.verify_calls, [])


if __name__ == "__main__":
    unittest.main()
