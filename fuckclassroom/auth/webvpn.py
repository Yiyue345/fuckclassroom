"""Shared GUET WebVPN gateway session for trusted in-process plugins.

This module is deliberately independent of the undergraduate academic website.
No browser cookie, credential, CAS ticket or Playwright state is exposed by status.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from .cas import (
    LoginVerificationBroker, LoginVerificationError, is_sms_verification_page,
    login_interaction_reason, run_sms_verification, submit_saved_credentials,
)
from .credentials import CredentialStoreError, SelectionCredentialStore
from ..core.atomic import atomic_write_text
from ..core.config import AppConfig
from ..core.plugins import PluginServiceError


GATEWAY_URL = "https://v.guet.edu.cn/"
GATEWAY_HOST = "v.guet.edu.cn"
CAS_HOST = "cas.guet.edu.cn"
WebVpnState = Literal[
    "unknown", "authenticated", "expired", "login_required",
    "verification_required", "unreachable", "recovering", "failed", "logged_out",
]

# Shared by academic and browser plugins, including their isolated worker processes.
_state_locks_guard = threading.Lock()
_state_locks: dict[str, threading.RLock] = {}


def _state_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _state_locks_guard:
        return _state_locks.setdefault(key, threading.RLock())


@contextmanager
def _locked_storage_file(path: Path):
    """An advisory lock for atomic cross-process read/merge/write on Win/POSIX."""
    with _state_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        with lock_path.open("a+b") as lock_file:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                if not lock_file.read(1):
                    lock_file.seek(0)
                    lock_file.write(b"0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class WebVpnSessionError(PluginServiceError):
    """Invalid browser handoff or unsafe URL, without private session data."""


@dataclass(frozen=True)
class WebVpnStatus:
    state: WebVpnState
    reason: str
    checked_at: str | None = None
    retry_after: int = 0

    @property
    def is_authenticated(self) -> bool:
        return self.state == "authenticated"


def validate_webvpn_url(url: str, *, allowed_origins: tuple[str, ...] = ()) -> str:
    """Only an HTTPS gateway URL or an explicitly permitted HTTPS campus origin."""
    try:
        parsed = urlsplit(url)
        if (url != url.strip() or parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.port not in (None, 443)
                or "\\" in url or any(ord(c) < 32 for c in url)):
            raise ValueError
        origin = f"https://{parsed.hostname.lower()}"
        allowed = {GATEWAY_URL.rstrip("/")}
        for candidate in allowed_origins:
            candidate_url = urlsplit(candidate)
            if (candidate_url.scheme != "https" or candidate_url.username
                    or candidate_url.password or candidate_url.port not in (None, 443)
                    or not candidate_url.hostname
                    or not candidate_url.hostname.endswith(".guet.edu.cn")):
                raise WebVpnSessionError("校园入口必须是明确的 HTTPS 桂电域名")
            allowed.add(f"https://{candidate_url.hostname.lower()}")
        if origin not in allowed:
            raise ValueError
        return url
    except (ValueError, AttributeError) as exc:
        raise WebVpnSessionError("导航地址不在允许的 WebVPN/校园入口范围") from exc


def safe_history_url(url: str, *, allowed_origins: tuple[str, ...] = ()) -> str:
    """Drop ALL query/fragment parameters (including CAS tickets and app tokens)."""
    validate_webvpn_url(url, allowed_origins=allowed_origins)
    parts = urlsplit(url)
    if any(word in parts.path.lower() for word in ("/authserver/", "/logout", "/login")):
        raise WebVpnSessionError("认证和注销页面不能加入历史或收藏")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _has_gateway_cookie(state: dict[str, Any]) -> bool:
    return any(
        isinstance(cookie, dict)
        and (cookie.get("domain") or "").lstrip(".").lower() == GATEWAY_HOST
        and bool(cookie.get("value"))
        for cookie in state.get("cookies", [])
    )


def _read_storage_unlocked(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("cookies", []), list):
            return {"cookies": [], "origins": []}
        return raw
    except (OSError, ValueError, TypeError):
        return {"cookies": [], "origins": []}


def read_storage_state(path: Path) -> dict[str, Any]:
    """Atomic rename makes lock-free cross-process reads safe and side-effect-free."""
    with _state_lock(path):
        return _read_storage_unlocked(path)


def save_storage_state(path: Path, state: dict[str, Any]) -> None:
    """Merge session domains and atomically replace storage_state.json.

    This prevents the gateway browser from dropping unrelated JW cookies, and
    the academic browser from dropping gateway cookies. Caller must not persist
    after an explicit logout. Only trusted server-side code may call this.
    """
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise WebVpnSessionError("无效的浏览器会话结构")
    with _locked_storage_file(path):
        old = _read_storage_unlocked(path)
        cookies: dict[tuple[str, str, str], dict[str, Any]] = {}
        for entry in [*old.get("cookies", []), *state.get("cookies", [])]:
            if not isinstance(entry, dict):
                continue
            name, domain, cookie_path = entry.get("name"), entry.get("domain"), entry.get("path", "/")
            if isinstance(name, str) and isinstance(domain, str):
                cookies[(name, domain.lower(), str(cookie_path))] = entry
        origins = {}
        for entry in [*old.get("origins", []), *state.get("origins", [])]:
            if isinstance(entry, dict) and isinstance(entry.get("origin"), str):
                origins[entry["origin"]] = entry
        merged = {"cookies": list(cookies.values()), "origins": list(origins.values())}
        atomic_write_text(path, json.dumps(merged, ensure_ascii=False))


def _classify_gateway_page(page: Any, response: Any) -> WebVpnState:
    """Only the gateway landing/login surface determines gateway auth state.

    A target site's HTTP 403 must never be interpreted as an expired gateway.
    Fail closed when the gateway portal layout is unrecognized.
    """
    url = str(page.url)
    parts = urlsplit(url)
    host, path = (parts.hostname or "").lower(), parts.path.lower()
    if host == CAS_HOST or (host == GATEWAY_HOST and (
        path.startswith("/login") or path.startswith("/wengine-auth/login")
    )):
        return "expired"
    if host != GATEWAY_HOST:
        return "unknown"
    code = getattr(response, "status", None)
    if code in (401, 403):
        return "expired" if path.startswith("/login") else "unknown"
    try:
        marker = page.evaluate("""() => !!(
          document.querySelector('a[href*="logout"], a[href*="Logout"], [data-action="logout"]')
          || document.querySelector('a[href^="/https/"], a[href^="/http/"]')
          || document.querySelector('a[href*="/https/777"], a[href*="/http/777"]')
        )""")
        if marker:
            return "authenticated"
        if page.locator('#pwdFromId #password, input[type="password"]').count():
            return "expired"
    except Exception:
        pass
    return "unknown"


class WebVpnSessionService:
    """Synchronous, thread-safe service; call via asyncio.to_thread in async routes.

    The process-local shared login lock serializes CAS work with existing feature
    plugins. A separate recovery lock collapses concurrent recovery requests.
    """

    def __init__(
        self, config: AppConfig, *, credential_store: SelectionCredentialStore,
        verification_broker: LoginVerificationBroker,
        login_lock: threading.Lock | threading.RLock,
        recovery_timeout: float = 90.0, cooldown: float = 30.0,
    ) -> None:
        self.config = config
        self.storage_path = config.data_dir / "webvpn" / "storage_state.json"
        self.meta_path = config.data_dir / "webvpn" / "session_meta.json"
        self.status_path = config.data_dir / "webvpn" / "gateway_status.json"
        self.credential_store = credential_store
        self.verification_broker = verification_broker
        self._login_lock = login_lock
        self._recovery_lock = threading.Lock()
        self._recovery_timeout = max(5.0, recovery_timeout)
        self._cooldown = max(0.0, cooldown)
        self._last_attempt = 0.0
        self._last_verified = 0.0
        self._status = WebVpnStatus("unknown", "尚未验证 WebVPN 网关")
        self._logged_out = False
        try:
            payload = json.loads(self.status_path.read_text(encoding="utf-8"))
            self._logged_out = payload.get("state") == "logged_out"
            if self._logged_out:
                self._status = WebVpnStatus("logged_out", "已主动退出，请手动重新连接")
        except (OSError, ValueError, AttributeError):
            pass

    def _set(self, state: WebVpnState, reason: str, *, checked: bool = False) -> WebVpnStatus:
        status = WebVpnStatus(state, reason, _now() if checked else self._status.checked_at)
        self._status = status
        atomic_write_text(self.status_path, json.dumps(asdict(status), ensure_ascii=False))
        return status

    def get_status(self) -> WebVpnStatus:
        """Side-effect-free snapshot: no HTTP requests and no cookie disclosure."""
        if self._logged_out:
            return WebVpnStatus("logged_out", "已主动退出，请手动重新连接", self._status.checked_at)
        if not _has_gateway_cookie(read_storage_state(self.storage_path)):
            if self._status.state == "authenticated":
                return WebVpnStatus("unknown", "未找到网关 Cookie", self._status.checked_at)
            if self._status.state == "unknown":
                return WebVpnStatus("login_required", "尚未保存 WebVPN 网关会话")
        remaining = max(0.0, self._cooldown - (time.monotonic() - self._last_attempt))
        if self._last_attempt and not self._status.is_authenticated:
            return WebVpnStatus(
                self._status.state, self._status.reason,
                self._status.checked_at, math.ceil(remaining),
            )
        return self._status

    def _probe(self) -> WebVpnStatus:
        if not _has_gateway_cookie(read_storage_state(self.storage_path)):
            return self._set("login_required", "未找到 WebVPN 网关 Cookie", checked=True)
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = self.new_browser_context(browser)
                    page = context.new_page()
                    response = page.goto(GATEWAY_URL, wait_until="domcontentloaded", timeout=15_000)
                    page.wait_for_timeout(900)
                    classification = _classify_gateway_page(page, response)
                    if classification == "authenticated":
                        self.save_browser_state(context)
                finally:
                    browser.close()
        except Exception:
            return self._set("unreachable", "无法连接 WebVPN 网关或浏览器启动失败", checked=True)
        if classification == "authenticated":
            self._last_verified = time.monotonic()
            return self._set("authenticated", "WebVPN 网关登录有效", checked=True)
        if classification == "expired":
            return self._set("expired", "WebVPN 网关会话已过期", checked=True)
        return self._set("unknown", "无法确认网关登录状态，未触发自动重登", checked=True)

    def verify(self) -> WebVpnStatus:
        if self._logged_out:
            return self.get_status()
        if not self._login_lock.acquire(timeout=self._recovery_timeout):
            return self._set("recovering", "其他登录流程仍在进行中")
        try:
            return self._probe()
        finally:
            self._login_lock.release()

    def ensure_authenticated(self, interactive_allowed: bool = True) -> WebVpnStatus:
        if self._logged_out:
            return self.get_status()
        if self._status.is_authenticated and time.monotonic() - self._last_verified < 30:
            return self.get_status()
        # All concurrent callers use this single flight; the second entrant
        # observes its result rather than launching another Chromium/login.
        if not self._recovery_lock.acquire(timeout=self._recovery_timeout):
            return WebVpnStatus("recovering", "等待 WebVPN 会话恢复超时")
        try:
            if self._logged_out:
                return self.get_status()
            if self._status.is_authenticated and time.monotonic() - self._last_verified < 30:
                return self.get_status()
            now = time.monotonic()
            if self._last_attempt and now - self._last_attempt < self._cooldown:
                return self.get_status()
            if not self._login_lock.acquire(timeout=self._recovery_timeout):
                return self._set("recovering", "共享认证锁等待超时")
            try:
                current = self._probe()
                if current.is_authenticated:
                    return current
                if current.state in {"unknown", "unreachable"}:
                    self._last_attempt = time.monotonic()
                    return current
                self._last_attempt = time.monotonic()
                try:
                    credentials = self.credential_store.load()
                except CredentialStoreError:
                    return self._set("login_required", "统一凭据不可用，请重新保存账号")
                if credentials is None and not interactive_allowed:
                    return self._set("login_required", "请先保存统一账号或手动登录")
                self._set("recovering", "正在恢复 WebVPN 网关会话")
                return self._login(credentials, interactive_allowed=interactive_allowed)
            finally:
                self._login_lock.release()
        finally:
            self._recovery_lock.release()

    def _login(self, credentials: Any, *, interactive_allowed: bool) -> WebVpnStatus:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=not interactive_allowed)
                try:
                    context = self.new_browser_context(browser)
                    page = context.new_page()
                    response = page.goto(GATEWAY_URL, wait_until="domcontentloaded", timeout=15_000)
                    submitted = bool(credentials and submit_saved_credentials(page, credentials))
                    submitted_at = time.monotonic()
                    deadline = submitted_at + min(self._recovery_timeout, self.config.login_timeout_seconds)
                    while time.monotonic() < deadline:
                        # CAS can render the credential form only *after* its SSO
                        # redirect, so a single submission immediately after goto
                        # is insufficient. Never submit more than once per attempt.
                        if credentials and not submitted:
                            submitted = bool(submit_saved_credentials(page, credentials))
                            if submitted:
                                submitted_at = time.monotonic()
                        outcome = _classify_gateway_page(page, response)
                        if outcome == "authenticated":
                            self.save_browser_state(context)
                            self._last_verified = time.monotonic()
                            return self._set("authenticated", "WebVPN 网关会话恢复成功", checked=True)
                        if credentials and is_sms_verification_page(page):
                            if not interactive_allowed:
                                return self._set("verification_required", "统一认证需要短信验证")
                            run_sms_verification(page, credentials.username, self.verification_broker, "WebVPN")
                            response = page.goto(GATEWAY_URL, wait_until="domcontentloaded", timeout=15_000)
                            continue
                        reason = login_interaction_reason(page)
                        if reason:
                            if not interactive_allowed:
                                return self._set("verification_required", "统一认证需要人工验证")
                        if not interactive_allowed and (not submitted or (outcome == "expired" and time.monotonic() - submitted_at >= 8)):
                            return self._set("verification_required" if credentials else "login_required",
                                             "需要在浏览器中完成学校统一身份认证")
                        page.wait_for_timeout(500)
                    return self._set("verification_required" if interactive_allowed else "failed",
                                     "登录超时，请手动完成学校统一认证")
                finally:
                    browser.close()
        except LoginVerificationError:
            return self._set("verification_required", "短信验证已取消或未完成")
        except Exception:
            return self._set("unreachable", "登录流程无法启动或网关连接失败")

    def reconnect(self, *, interactive_allowed: bool = True) -> WebVpnStatus:
        """Explicit user action after logout; never called by automatic recovery."""
        with self._recovery_lock:
            self._logged_out = False
            self._last_attempt = 0.0
            self._set("login_required", "用户请求重新连接 WebVPN")
        return self.ensure_authenticated(interactive_allowed=interactive_allowed)

    def new_browser_context(self, browser: Any) -> Any:
        """Trusted in_process plugin only. Do not return this context via RPC."""
        state = read_storage_state(self.storage_path)
        return browser.new_context(storage_state=state) if state.get("cookies") else browser.new_context()

    def save_browser_state(self, browser_context: Any) -> None:
        """Trusted in_process browser handoff; cookies stay server-side."""
        with _state_lock(self.storage_path):
            if self._logged_out:
                raise WebVpnSessionError("用户已主动退出，禁止保存旧会话")
            save_storage_state(self.storage_path, browser_context.storage_state())

    def clear_session(self) -> None:
        """Explicit logout, preserving user credentials but preventing auto-relogin."""
        with self._recovery_lock:
            with self._login_lock:
                self._logged_out = True
                self._last_verified = 0.0
                with _locked_storage_file(self.storage_path):
                    for path in (self.storage_path, self.meta_path):
                        path.unlink(missing_ok=True)
                self._set("logged_out", "已主动退出，请手动重新连接", checked=True)

    logout = clear_session


__all__ = [
    "GATEWAY_URL", "WebVpnStatus", "WebVpnSessionService", "WebVpnSessionError",
    "validate_webvpn_url", "safe_history_url", "save_storage_state", "read_storage_state",
]
