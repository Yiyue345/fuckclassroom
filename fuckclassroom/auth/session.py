from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .cas import (
    LoginVerificationBroker,
    is_sms_verification_page,
    login_interaction_reason,
    run_sms_verification,
    submit_saved_credentials,
)
from .credentials import CredentialStoreError, SelectionCredentialStore, SelectionCredentials
from ..core.config import AppConfig


CLASSROOM_HOST = "classroom.guet.edu.cn"
CAS_HOST = "cas.guet.edu.cn"
COURSE_PROBE_PATH = "/personal/courseapi/vlabpassportapi/v1/account-profile/course?nowpage=1&per-page=1"
LOGIN_PROBE_INTERVAL_MS = 1_000
ProgressCallback = Callable[[int, str], None]


@dataclass(frozen=True)
class SessionStatus:
    is_logged_in: bool
    message: str
    final_url: str | None = None
    saved_at: str | None = None
    cookie_count: int = 0


@dataclass(frozen=True)
class LoginRunStatus:
    state: str
    message: str
    updated_at: str
    final_url: str | None = None


class LoginSessionService:
    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        credential_store: SelectionCredentialStore | None = None,
        verification_broker: LoginVerificationBroker | None = None,
        login_lock: threading.Lock | None = None,
    ) -> None:
        self.config = config or AppConfig()
        self.credential_store = credential_store
        self.verification_broker = verification_broker or LoginVerificationBroker()
        self._login_lock = login_lock or threading.Lock()

    def get_session_status(self) -> SessionStatus:
        if not self.config.storage_state_path.exists():
            return SessionStatus(False, "尚未保存登录会话")

        try:
            storage_state = self._read_json(self.config.storage_state_path)
        except json.JSONDecodeError:
            return SessionStatus(False, "会话文件已损坏，请重新登录")

        cookies = storage_state.get("cookies", [])
        meta = self._read_json(self.config.session_meta_path, default={})
        login_run = self.get_login_run_status()
        is_logged_in = bool(cookies)
        message = "已保存登录会话" if cookies else "会话文件中没有 cookie，请重新登录"
        final_url = meta.get("final_url")
        if cookies and login_run.state == "failed":
            is_logged_in = False
            message = login_run.message or "登录会话已失效，请重新登录"
            final_url = login_run.final_url or final_url
        elif cookies and login_run.state == "succeeded":
            message = login_run.message or "登录会话有效"
            final_url = login_run.final_url or final_url
        return SessionStatus(
            is_logged_in=is_logged_in,
            message=message,
            final_url=final_url,
            saved_at=meta.get("saved_at"),
            cookie_count=len(cookies),
        )

    def record_session_verification(
        self,
        is_valid: bool,
        message: str,
        final_url: str | None = None,
    ) -> SessionStatus:
        self._write_login_status("succeeded" if is_valid else "failed", message, final_url)
        return self.get_session_status()

    def get_login_run_status(self) -> LoginRunStatus:
        payload = self._read_json(self.config.login_status_path, default=None)
        if not payload:
            return LoginRunStatus("idle", "尚未开始登录", self._now())

        return LoginRunStatus(
            state=str(payload.get("state", "idle")),
            message=str(payload.get("message", "")),
            updated_at=str(payload.get("updated_at", self._now())),
            final_url=payload.get("final_url"),
        )

    def mark_login_started(self) -> None:
        self._write_login_status("running", "正在通过学校统一身份认证登录课程录播")

    def start_browser_login(self, progress: ProgressCallback | None = None) -> SessionStatus:
        self._ensure_auth_dir()
        self.mark_login_started()
        with self._login_lock:
            try:
                credentials = self._load_credentials()
            except CredentialStoreError as exc:
                message = str(exc)
                self._write_login_status("failed", message)
                return SessionStatus(False, message)
            if credentials is not None:
                _report(progress, 5, "正在使用统一桂电账号自动登录课程录播")
                status = self._start_browser_login(credentials, progress, headless=True)
                if status.is_logged_in:
                    return status
                _report(progress, 16, f"自动登录需要人工确认：{status.message}")
            _report(progress, 20, "正在打开统一身份认证窗口")
            return self._start_browser_login(credentials, progress, headless=False)

    def login_for_web(self, progress: ProgressCallback | None = None) -> str:
        status = self.start_browser_login(progress)
        if not status.is_logged_in:
            raise RuntimeError(status.message)
        return "/classroom#session"

    def _start_browser_login(self, credentials: SelectionCredentials | None, progress: ProgressCallback | None, *, headless: bool) -> SessionStatus:
        self._ensure_auth_dir()

        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            message = "缺少 Playwright 依赖，请先安装 requirements.txt 并执行 playwright install chromium"
            self._write_login_status("failed", message)
            return SessionStatus(False, message)

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=headless)
                context = self._new_login_context(browser)
                page = context.new_page()
                page.goto(self.config.site_url, wait_until="domcontentloaded")
                submitted = bool(credentials and submit_saved_credentials(page, credentials))

                final_url = self._wait_for_authenticated_classroom(page, PlaywrightTimeoutError, credentials, progress, headless, submitted)
                context.storage_state(path=str(self.config.storage_state_path))
                browser.close()
        except PlaywrightTimeoutError:
            message = "登录等待超时，请重新点击登录并在浏览器中完成认证"
            self._write_login_status("failed", message)
            return SessionStatus(False, message)
        except Exception as exc:  # noqa: BLE001 - surfaced to the local user.
            message = f"登录流程启动失败：{exc}"
            self._write_login_status("failed", message)
            return SessionStatus(False, message)

        storage_state = self._read_json(self.config.storage_state_path, default={})
        cookie_count = len(storage_state.get("cookies", []))
        self._write_session_meta(final_url)
        message = "课程录播登录成功，会话已保存" if cookie_count else "未获取到 cookie，请重新登录"
        state = "succeeded" if cookie_count else "failed"
        self._write_login_status(state, message, final_url)
        return SessionStatus(
            is_logged_in=cookie_count > 0,
            message=message,
            final_url=final_url,
            saved_at=self._now(),
            cookie_count=cookie_count,
        )

    def verify_for_web(self, progress: ProgressCallback | None = None) -> str:
        _report(progress, 5, "正在验证课程录播会话")
        status = self.verify_saved_session(progress=progress)
        if not status.is_logged_in:
            raise RuntimeError(status.message)
        _report(progress, 100, status.message)
        return "/classroom#session"

    def verify_saved_session(
        self,
        *,
        auto_relogin: bool = True,
        progress: ProgressCallback | None = None,
    ) -> SessionStatus:
        with self._login_lock:
            status = self._verify_saved_session_once()
            if status.is_logged_in or not auto_relogin:
                return status
            try:
                credentials = self._load_credentials()
            except CredentialStoreError as exc:
                self._write_login_status("failed", str(exc))
                return SessionStatus(False, str(exc), final_url=status.final_url)
            if credentials is None:
                return status
            _report(progress, 20, "课程录播会话已失效，正在使用统一桂电账号恢复")
            relogin = self._start_browser_login(credentials, progress, headless=True)
            if relogin.is_logged_in:
                return relogin
            message = f"课程录播自动登录失败：{relogin.message}"
            self._write_login_status("failed", message, status.final_url)
            return SessionStatus(False, message, final_url=status.final_url)

    def _verify_saved_session_once(self) -> SessionStatus:
        if not self.config.storage_state_path.exists():
            status = SessionStatus(False, "尚未保存登录会话")
            self._write_login_status("failed", status.message)
            return status

        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError:
            status = SessionStatus(False, "缺少 Playwright 依赖，无法验证会话")
            self._write_login_status("failed", status.message)
            return status

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(storage_state=str(self.config.storage_state_path))
                page = context.new_page()
                page.goto(self.config.site_url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(3_000)
                final_url = page.url
                authenticated = self._is_authenticated_classroom(page)
                browser.close()
        except PlaywrightTimeoutError:
            status = SessionStatus(False, "访问平台超时，可能是网络或会话问题")
            self._write_login_status("failed", status.message)
            return status
        except Exception as exc:  # noqa: BLE001 - surfaced to the local user.
            status = SessionStatus(False, f"会话验证失败：{exc}")
            self._write_login_status("failed", status.message)
            return status

        host = urlparse(final_url).hostname or ""
        if host == CAS_HOST:
            status = SessionStatus(False, "会话已失效，需要重新登录", final_url=final_url)
            self._write_login_status("failed", status.message, final_url)
            return status
        if host == CLASSROOM_HOST and authenticated:
            meta = self._read_json(self.config.session_meta_path, default={})
            storage_state = self._read_json(self.config.storage_state_path, default={})
            cookies = storage_state.get("cookies", [])
            status = SessionStatus(
                True,
                "登录会话有效",
                final_url=final_url,
                saved_at=meta.get("saved_at"),
                cookie_count=len(cookies),
            )
            self._write_login_status("succeeded", status.message, final_url)
            return status
        if host == CLASSROOM_HOST:
            status = SessionStatus(False, "会话已失效或平台认证失败，需要重新登录", final_url=final_url)
            self._write_login_status("failed", status.message, final_url)
            return status
        status = SessionStatus(False, f"登录后跳转到了未知站点：{host}", final_url=final_url)
        self._write_login_status("failed", status.message, final_url)
        return status

    def _load_credentials(self) -> SelectionCredentials | None:
        if self.credential_store is None:
            return None
        return self.credential_store.load()

    def _new_login_context(self, browser: Any) -> Any:
        if self.config.storage_state_path.exists():
            try:
                return browser.new_context(storage_state=str(self.config.storage_state_path))
            except Exception:
                pass
        return browser.new_context()

    def clear_session(self) -> None:
        for path in (
            self.config.storage_state_path,
            self.config.session_meta_path,
            self.config.login_status_path,
        ):
            if path.exists():
                path.unlink()

    def _wait_for_authenticated_classroom(
        self,
        page: Any,
        timeout_error: type[Exception],
        credentials: SelectionCredentials | None,
        progress: ProgressCallback | None,
        headless: bool,
        submitted: bool,
    ) -> str:
        deadline = datetime.now(timezone.utc).timestamp() + self.config.login_timeout_seconds
        last_url = page.url
        while datetime.now(timezone.utc).timestamp() < deadline:
            if page.is_closed():
                raise timeout_error("登录窗口已关闭")
            last_url = page.url
            host = urlparse(last_url).hostname or ""
            if host == CLASSROOM_HOST and self._is_authenticated_classroom(page):
                return last_url
            if credentials and is_sms_verification_page(page):
                run_sms_verification(
                    page,
                    credentials.username,
                    self.verification_broker,
                    "课程录播",
                    progress,
                )
                page.goto(self.config.site_url, wait_until="domcontentloaded", timeout=30_000)
                submitted = submit_saved_credentials(page, credentials) or submitted
                continue
            reason = login_interaction_reason(page) if submitted else ""
            if reason and headless:
                raise RuntimeError(reason)
            message = f"{reason}，请在浏览器中完成后继续" if reason else "等待学校统一身份认证完成"
            self._write_login_status("running", message, last_url)
            _report(progress, 30 if headless else 25, message)
            page.wait_for_timeout(LOGIN_PROBE_INTERVAL_MS)
        raise timeout_error(f"CAS login did not finish before timeout, last url: {last_url}")

    @staticmethod
    def _is_authenticated_classroom(page: Any) -> bool:
        host = urlparse(page.url).hostname or ""
        if host != CLASSROOM_HOST:
            return False
        try:
            return bool(
                page.evaluate(
                    """
                    async (path) => {
                      const response = await fetch(path, {
                        credentials: "include",
                        headers: { Accept: "application/json" }
                      });
                      const text = await response.text();
                      if (!response.ok) return false;
                      try {
                        const payload = JSON.parse(text);
                        return payload && (payload.status === 200 || payload.code === 1000);
                      } catch (_) {
                        return false;
                      }
                    }
                    """,
                    COURSE_PROBE_PATH,
                )
            )
        except Exception:
            return False

    def _write_session_meta(self, final_url: str) -> None:
        self._ensure_auth_dir()
        payload = {
            "saved_at": self._now(),
            "site_url": self.config.site_url,
            "final_url": final_url,
        }
        self.config.session_meta_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_login_status(self, state: str, message: str, final_url: str | None = None) -> None:
        self._ensure_auth_dir()
        payload = asdict(LoginRunStatus(state, message, self._now(), final_url))
        self.config.login_status_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _ensure_auth_dir(self) -> None:
        self.config.auth_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read_json(path: Path, default: Any = None) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _report(progress: ProgressCallback | None, value: int, message: str) -> None:
    if progress:
        progress(value, message)
