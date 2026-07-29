from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from fuckclassroom.auth import LoginSessionService, SessionStatus
from fuckclassroom.auth.cas import (
    LoginVerificationBroker,
    LoginVerificationError,
    run_sms_verification,
    submit_saved_credentials,
)
from fuckclassroom.config import AppConfig
from fuckclassroom.auth.credentials import SelectionCredentials
from fuckclassroom.web import _unified_credential_path


class _FakeCasPage:
    def evaluate(self, _script: str, arguments: dict[str, str]) -> dict[str, object]:
        if "getDynamicCodeByReauth" in arguments["path"]:
            return {
                "res": "success",
                "mobile": "123****4567",
                "returnMessage": "动态口令已发送到手机",
                "codeTime": 2,
            }
        self.submitted_code = arguments["code"]
        return {"code": "reAuth_success"}


class _SavedCredentialStore:
    def load(self) -> SelectionCredentials:
        return SelectionCredentials("student-id", "saved-password")


class _DelayedLoginLocator:
    def __init__(self, page, selector: str) -> None:
        self.page = page
        self.selector = selector

    def wait_for(self, *, state: str, timeout: int) -> None:
        self.page.waited.append((self.selector, state, timeout))

    def is_visible(self) -> bool:
        return True

    def fill(self, value: str) -> None:
        self.page.filled[self.selector] = value

    def count(self) -> int:
        return 1

    def is_checked(self) -> bool:
        return False

    def check(self) -> None:
        self.page.remembered = True

    def click(self) -> None:
        self.page.submitted = True


class _DelayedLoginPage:
    def __init__(self) -> None:
        self.waited: list[tuple[str, str, int]] = []
        self.filled: dict[str, str] = {}
        self.remembered = False
        self.submitted = False

    def locator(self, selector: str) -> _DelayedLoginLocator:
        return _DelayedLoginLocator(self, selector)


class _ManualLoginService(LoginSessionService):
    def start_browser_login(self, progress=None, *, force_interactive: bool = False):
        self.force_interactive = force_interactive
        return SessionStatus(True, "logged in", cookie_count=1)


class _AutoReloginService(LoginSessionService):
    def _verify_saved_session_once(self) -> SessionStatus:
        return SessionStatus(False, "expired")

    def _start_browser_login(self, credentials, progress, *, headless):
        self.used_credentials = credentials
        self.used_headless = headless
        return SessionStatus(True, "restored", cookie_count=1)


class AuthVerificationTests(unittest.TestCase):
    def test_saved_credentials_wait_for_delayed_cas_form(self) -> None:
        page = _DelayedLoginPage()

        submitted = submit_saved_credentials(page, SelectionCredentials("student-id", "saved-password"))

        self.assertTrue(submitted)
        self.assertEqual(len(page.waited), 3)
        self.assertTrue(all(item[2] == 5_000 for item in page.waited))
        self.assertTrue(page.remembered)
        self.assertTrue(page.submitted)

    def test_explicit_classroom_login_forces_visible_flow(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            service = _ManualLoginService(AppConfig(data_dir=Path(directory)))

            result = service.login_for_web()

            self.assertEqual(result, "/classroom#session")
            self.assertTrue(service.force_interactive)

    def test_sms_challenge_sends_and_verifies_in_worker_thread(self) -> None:
        broker = LoginVerificationBroker()
        page = _FakeCasPage()
        errors: list[Exception] = []

        def worker() -> None:
            try:
                run_sms_verification(page, "student-id", broker, "本科教务", timeout_seconds=2)
            except Exception as exc:  # pragma: no cover - asserted through errors.
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        challenge = self._wait_for_challenge(broker)
        broker.request_send(str(challenge["id"]))
        sent = self._wait_for_mobile(broker)
        self.assertEqual(sent["mobile"], "123****4567")
        self.assertGreater(sent["retry_after"], 0)
        broker.request_verify(str(challenge["id"]), "123456")
        thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(page.submitted_code, "123456")
        self.assertIsNone(broker.current())

    def test_sms_challenge_rejects_non_six_digit_code(self) -> None:
        broker = LoginVerificationBroker()
        challenge_id = broker.begin("课程录播")

        with self.assertRaises(LoginVerificationError):
            broker.request_verify(challenge_id, "12ab")

        broker.finish(challenge_id)

    def test_legacy_selection_credentials_move_to_unified_account_path(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = AppConfig(data_dir=Path(directory))
            legacy = config.data_dir / "webvpn" / "credentials.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("encrypted-envelope", encoding="utf-8")

            resolved = _unified_credential_path(config)

            self.assertEqual(resolved, config.data_dir / "account" / "credentials.json")
            self.assertEqual(resolved.read_text(encoding="utf-8"), "encrypted-envelope")
            self.assertFalse(legacy.exists())

    def test_classroom_session_auto_relogin_uses_shared_credentials(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            service = _AutoReloginService(
                AppConfig(data_dir=Path(directory)),
                credential_store=_SavedCredentialStore(),
            )

            status = service.verify_saved_session()

            self.assertTrue(status.is_logged_in)
            self.assertEqual(service.used_credentials.username, "student-id")
            self.assertTrue(service.used_headless)

    @staticmethod
    def _wait_for_challenge(broker: LoginVerificationBroker) -> dict[str, object]:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            challenge = broker.current()
            if challenge:
                return challenge
            time.sleep(0.01)
        raise AssertionError("verification challenge was not created")

    @staticmethod
    def _wait_for_mobile(broker: LoginVerificationBroker) -> dict[str, object]:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            challenge = broker.current()
            if challenge and challenge["mobile"]:
                return challenge
            time.sleep(0.01)
        raise AssertionError("dynamic code send result was not published")


if __name__ == "__main__":
    unittest.main()
