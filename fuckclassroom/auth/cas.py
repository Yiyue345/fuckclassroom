from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .credentials import SelectionCredentials


DYNAMIC_CODE_PATH = "/authserver/dynamicCode/getDynamicCodeByReauth.do"
REAUTH_SUBMIT_PATH = "/authserver/reAuthCheck/reAuthSubmit.do"
ProgressCallback = Callable[[int, str], None]


class LoginVerificationError(RuntimeError):
    pass


@dataclass
class VerificationChallenge:
    id: str
    system: str
    status: str = "waiting"
    message: str = "需要短信动态码才能继续登录"
    mobile: str = ""
    error: str = ""
    code_time: int = 0
    can_resend_at: float = 0.0


class LoginVerificationBroker:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._challenge: VerificationChallenge | None = None
        self._actions: list[tuple[str, str]] = []

    def begin(self, system: str) -> str:
        with self._condition:
            if self._challenge is not None:
                raise LoginVerificationError("已有登录验证码正在等待处理")
            challenge_id = uuid.uuid4().hex
            self._challenge = VerificationChallenge(challenge_id, system)
            self._actions.clear()
            self._condition.notify_all()
            return challenge_id

    def current(self) -> dict[str, object] | None:
        with self._condition:
            if self._challenge is None:
                return None
            payload = asdict(self._challenge)
            payload.pop("can_resend_at", None)
            payload["retry_after"] = max(0, int(self._challenge.can_resend_at - time.time() + 0.999))
            return payload

    def request_send(self, challenge_id: str) -> None:
        with self._condition:
            challenge = self._require(challenge_id)
            if time.time() < challenge.can_resend_at:
                raise LoginVerificationError("短信动态码暂时不能重复发送")
            self._actions.append(("send", ""))
            challenge.status = "sending"
            challenge.error = ""
            self._condition.notify_all()

    def request_verify(self, challenge_id: str, code: str) -> None:
        normalized = code.strip()
        if not re.fullmatch(r"\d{6}", normalized):
            raise LoginVerificationError("请输入 6 位短信动态码")
        with self._condition:
            challenge = self._require(challenge_id)
            self._actions.append(("verify", normalized))
            challenge.status = "verifying"
            challenge.error = ""
            self._condition.notify_all()

    def request_cancel(self, challenge_id: str) -> None:
        with self._condition:
            challenge = self._require(challenge_id)
            self._actions.append(("cancel", ""))
            challenge.status = "cancelled"
            self._condition.notify_all()

    def wait_for_action(self, challenge_id: str, timeout_seconds: float) -> tuple[str, str]:
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while True:
                self._require(challenge_id)
                if self._actions:
                    return self._actions.pop(0)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LoginVerificationError("等待短信动态码超时")
                self._condition.wait(min(remaining, 1.0))

    def sent(self, challenge_id: str, *, mobile: str, message: str, code_time: int) -> None:
        with self._condition:
            challenge = self._require(challenge_id)
            challenge.status = "waiting"
            challenge.mobile = mobile
            challenge.message = message or "短信动态码已发送"
            challenge.code_time = max(0, code_time)
            challenge.can_resend_at = time.time() + challenge.code_time
            challenge.error = ""
            self._condition.notify_all()

    def failed(self, challenge_id: str, message: str) -> None:
        with self._condition:
            challenge = self._require(challenge_id)
            challenge.status = "waiting"
            challenge.error = message
            self._condition.notify_all()

    def finish(self, challenge_id: str) -> None:
        with self._condition:
            self._require(challenge_id)
            self._challenge = None
            self._actions.clear()
            self._condition.notify_all()

    def _require(self, challenge_id: str) -> VerificationChallenge:
        if self._challenge is None or self._challenge.id != challenge_id:
            raise LoginVerificationError("验证码请求已失效，请重新登录")
        return self._challenge


def submit_saved_credentials(page: Any, credentials: SelectionCredentials) -> bool:
    try:
        username = page.locator("#pwdFromId #username")
        password = page.locator("#pwdFromId #password")
        submit = page.locator("#login_submit")
        if not username.is_visible() or not password.is_visible() or not submit.is_visible():
            return False
        username.fill(credentials.username)
        password.fill(credentials.password)
        remember_me = page.locator("#pwdFromId #rememberMe")
        if remember_me.count() and not remember_me.is_checked():
            remember_me.check()
        submit.click()
        return True
    except Exception:
        return False


def is_sms_verification_page(page: Any) -> bool:
    try:
        url = page.url.lower()
        return "reauthcheck" in url or "dynamicauth" in url
    except Exception:
        return False


def login_interaction_reason(page: Any) -> str:
    try:
        captcha = page.locator("#pwdFromId #captcha")
        if captcha.count() and captcha.is_visible():
            return "统一认证要求输入图形验证码"
        error_tip = page.locator("#showErrorTip")
        if error_tip.count() and error_tip.is_visible():
            message = error_tip.inner_text().strip()
            if message:
                return f"统一认证返回：{message}"
    except Exception:
        pass
    return ""


def run_sms_verification(
    page: Any,
    username: str,
    broker: LoginVerificationBroker,
    system: str,
    progress: ProgressCallback | None = None,
    *,
    timeout_seconds: float = 300,
) -> None:
    challenge_id = broker.begin(system)
    _report(progress, 42, "统一认证需要短信动态码，请在当前页面完成验证")
    try:
        while True:
            action, value = broker.wait_for_action(challenge_id, timeout_seconds)
            if action == "cancel":
                raise LoginVerificationError("已取消短信动态码验证")
            if action == "send":
                try:
                    result = page.evaluate(
                        """
                        async ({path, username}) => {
                          const marker = "/authserver/";
                          const prefix = location.pathname.includes(marker)
                            ? location.pathname.split(marker, 1)[0]
                            : "";
                          const body = new URLSearchParams({
                            userName: username,
                            authCodeTypeName: "reAuthDynamicCodeType"
                          });
                          const response = await fetch(prefix + path, {
                            method: "POST",
                            credentials: "include",
                            headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                            body: body.toString()
                          });
                          const text = await response.text();
                          if (!response.ok) throw new Error(text || `HTTP ${response.status}`);
                          return JSON.parse(text);
                        }
                        """,
                        {"path": DYNAMIC_CODE_PATH, "username": username},
                    )
                    if str(result.get("res", "")).lower() != "success":
                        raise LoginVerificationError(str(result.get("returnMessage") or "短信动态码发送失败"))
                    broker.sent(
                        challenge_id,
                        mobile=str(result.get("mobile") or ""),
                        message=str(result.get("returnMessage") or "短信动态码已发送"),
                        code_time=int(result.get("codeTime") or 0),
                    )
                except Exception as exc:  # noqa: BLE001 - shown in the local verification dialog.
                    broker.failed(challenge_id, f"发送失败：{exc}")
                continue
            if action == "verify":
                try:
                    result = page.evaluate(
                        """
                        async ({path, code}) => {
                          const marker = "/authserver/";
                          const prefix = location.pathname.includes(marker)
                            ? location.pathname.split(marker, 1)[0]
                            : "";
                          const body = new URLSearchParams({
                            service: "",
                            reAuthType: "3",
                            isMultifactor: "true",
                            password: "",
                            dynamicCode: code,
                            uuid: "",
                            answer1: "",
                            answer2: "",
                            otpCode: "",
                            skipTmpReAuth: "true"
                          });
                          const response = await fetch(prefix + path, {
                            method: "POST",
                            credentials: "include",
                            headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                            body: body.toString()
                          });
                          const text = await response.text();
                          if (!response.ok) throw new Error(text || `HTTP ${response.status}`);
                          return JSON.parse(text);
                        }
                        """,
                        {"path": REAUTH_SUBMIT_PATH, "code": value},
                    )
                    if str(result.get("code") or "") != "reAuth_success":
                        raise LoginVerificationError(str(result.get("msg") or "短信动态码校验失败"))
                    _report(progress, 52, "短信动态码验证成功，正在继续登录")
                    return
                except Exception as exc:  # noqa: BLE001 - shown in the local verification dialog.
                    broker.failed(challenge_id, f"验证失败：{exc}")
    finally:
        try:
            broker.finish(challenge_id)
        except LoginVerificationError:
            pass


def _report(progress: ProgressCallback | None, value: int, message: str) -> None:
    if progress:
        progress(value, message)
