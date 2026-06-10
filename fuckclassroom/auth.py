from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import AppConfig


CLASSROOM_HOST = "classroom.guet.edu.cn"
CAS_HOST = "cas.guet.edu.cn"


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
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig()

    def get_session_status(self) -> SessionStatus:
        if not self.config.storage_state_path.exists():
            return SessionStatus(False, "尚未保存登录会话")

        try:
            storage_state = self._read_json(self.config.storage_state_path)
        except json.JSONDecodeError:
            return SessionStatus(False, "会话文件已损坏，请重新登录")

        cookies = storage_state.get("cookies", [])
        meta = self._read_json(self.config.session_meta_path, default={})
        return SessionStatus(
            is_logged_in=bool(cookies),
            message="已保存登录会话" if cookies else "会话文件中没有 cookie，请重新登录",
            final_url=meta.get("final_url"),
            saved_at=meta.get("saved_at"),
            cookie_count=len(cookies),
        )

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
        self._write_login_status("running", "浏览器登录窗口已启动，请在窗口中完成学校统一身份认证")

    def start_browser_login(self) -> SessionStatus:
        self._ensure_auth_dir()
        self.mark_login_started()

        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            message = "缺少 Playwright 依赖，请先安装 requirements.txt 并执行 playwright install chromium"
            self._write_login_status("failed", message)
            return SessionStatus(False, message)

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.config.browser_headless)
                context = browser.new_context()
                page = context.new_page()
                page.goto(self.config.site_url, wait_until="domcontentloaded")

                final_url = self._wait_for_classroom_page(page, PlaywrightTimeoutError)
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

        status = self.get_session_status()
        self._write_session_meta(final_url)
        message = "登录会话已保存，可以关闭浏览器窗口" if status.cookie_count else "未获取到 cookie，请重新登录"
        state = "succeeded" if status.cookie_count else "failed"
        self._write_login_status(state, message, final_url)
        return SessionStatus(
            is_logged_in=status.cookie_count > 0,
            message=message,
            final_url=final_url,
            saved_at=self._now(),
            cookie_count=status.cookie_count,
        )

    def verify_saved_session(self) -> SessionStatus:
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
        if host == CLASSROOM_HOST:
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
        status = SessionStatus(False, f"登录后跳转到了未知站点：{host}", final_url=final_url)
        self._write_login_status("failed", status.message, final_url)
        return status

    def clear_session(self) -> None:
        for path in (
            self.config.storage_state_path,
            self.config.session_meta_path,
            self.config.login_status_path,
        ):
            if path.exists():
                path.unlink()

    def _wait_for_classroom_page(self, page: Any, timeout_error: type[Exception]) -> str:
        deadline_ms = self.config.login_timeout_seconds * 1000
        page.wait_for_function(
            """
            () => {
              const host = window.location.hostname;
              return host === "classroom.guet.edu.cn";
            }
            """,
            timeout=deadline_ms,
        )
        final_url = page.url
        host = urlparse(final_url).hostname or ""
        if host != CLASSROOM_HOST:
            raise timeout_error("CAS login did not finish on classroom host")
        return final_url

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
