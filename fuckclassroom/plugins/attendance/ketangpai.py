from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ...auth.credentials import (
    CredentialStoreError,
    _protect_with_dpapi,
    _unprotect_with_dpapi,
)


KETANGPAI_BASE_URL = "https://openapiv5.ketangpai.com"
_LOGIN_KEY = b"ktp4567890123456"
_PHONE_RE = re.compile(r"^1\d{10}$")


class KetangpaiError(RuntimeError):
    pass


class KetangpaiAuthenticationError(KetangpaiError):
    pass


@dataclass(frozen=True)
class KetangpaiCredentials:
    account: str
    token: str
    password: str = ""
    login_method: str = "password"


@dataclass(frozen=True)
class KetangpaiSessionStatus:
    is_saved: bool
    is_valid: bool | None
    account: str
    login_method: str
    message: str


class KetangpaiCredentialStore:
    """DPAPI-protected storage kept separate from the GUET account."""

    def __init__(
        self,
        path: Path,
        *,
        protect: Callable[[bytes], bytes] | None = None,
        unprotect: Callable[[bytes], bytes] | None = None,
    ) -> None:
        self.path = path
        self._protect = protect or _protect_with_dpapi
        self._unprotect = unprotect or _unprotect_with_dpapi

    def load(self) -> KetangpaiCredentials | None:
        if not self.path.exists():
            return None
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            protected = base64.b64decode(str(envelope["protected"]), validate=True)
            payload = json.loads(self._unprotect(protected).decode("utf-8"))
            credentials = KetangpaiCredentials(
                account=str(payload.get("account") or "").strip(),
                token=str(payload.get("token") or "").strip(),
                password=str(payload.get("password") or ""),
                login_method=str(payload.get("login_method") or "password"),
            )
        except (OSError, KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError("课堂派登录会话无法读取，请重新登录") from exc
        if not credentials.account or not credentials.token:
            raise CredentialStoreError("课堂派登录会话不完整，请重新登录")
        return credentials

    def save(
        self,
        account: str,
        token: str,
        *,
        password: str = "",
        login_method: str = "password",
    ) -> KetangpaiCredentials:
        credentials = KetangpaiCredentials(
            account=account.strip(),
            token=token.strip(),
            password=password,
            login_method=login_method,
        )
        if not credentials.account or not credentials.token:
            raise CredentialStoreError("课堂派账号和登录令牌不能为空")
        plaintext = json.dumps(credentials.__dict__, ensure_ascii=False).encode("utf-8")
        envelope = {
            "version": 1,
            "protected": base64.b64encode(self._protect(plaintext)).decode("ascii"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise CredentialStoreError(f"课堂派登录会话保存失败：{exc}") from exc
        return credentials

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise CredentialStoreError(f"课堂派登录会话删除失败：{exc}") from exc

    def public_status(self) -> KetangpaiSessionStatus:
        try:
            credentials = self.load()
        except CredentialStoreError as exc:
            return KetangpaiSessionStatus(False, False, "", "", str(exc))
        if credentials is None:
            return KetangpaiSessionStatus(False, None, "", "", "尚未登录课堂派")
        return KetangpaiSessionStatus(
            True,
            None,
            credentials.account,
            credentials.login_method,
            "已加密保存课堂派会话，尚未验证",
        )


class KetangpaiClient:
    def __init__(self, store: KetangpaiCredentialStore) -> None:
        self.store = store
        self._last_status: KetangpaiSessionStatus | None = None

    def get_session_status(self) -> KetangpaiSessionStatus:
        stored = self.store.public_status()
        if self._last_status and stored.is_saved:
            return self._last_status
        return stored

    def login_password(self, account: str, password: str) -> KetangpaiSessionStatus:
        account = account.strip()
        if not account or not password:
            raise KetangpaiAuthenticationError("课堂派账号和密码不能为空")
        token = self._login(account, password=password, endpoint="/UserApi/login")
        self.store.save(account, token, password=password, login_method="password")
        self._last_status = KetangpaiSessionStatus(
            True, True, account, "password", "课堂派登录成功，会话已加密保存"
        )
        return self._last_status

    def login_sms(self, mobile: str, code: str) -> KetangpaiSessionStatus:
        mobile = self._validate_mobile(mobile)
        if not code.strip():
            raise KetangpaiAuthenticationError("短信验证码不能为空")
        token = self._login(
            mobile,
            code=code.strip(),
            endpoint="/UserApi/loginByMobile",
        )
        self.store.save(mobile, token, login_method="sms")
        self._last_status = KetangpaiSessionStatus(
            True, True, mobile, "sms", "课堂派短信登录成功，会话已加密保存"
        )
        return self._last_status

    def request_sms_captcha(self, session_id: str) -> tuple[bytes, str]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{12,80}", session_id):
            raise KetangpaiError("课堂派图形验证码会话无效")
        request = Request(
            f"{KETANGPAI_BASE_URL}/UserApi/verify?{urlencode({'sessionid': session_id})}",
            headers={"Accept": "image/*", "User-Agent": "FuckClassroom/1.0"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                content = response.read(2 * 1024 * 1024 + 1)
                content_type = response.headers.get_content_type()
        except (HTTPError, URLError) as exc:
            raise KetangpaiError(f"课堂派图形验证码获取失败：{getattr(exc, 'reason', exc)}") from exc
        if len(content) > 2 * 1024 * 1024 or not content_type.startswith("image/"):
            raise KetangpaiError("课堂派图形验证码响应异常")
        return content, content_type

    def send_sms_code(self, mobile: str, captcha: str, session_id: str) -> str:
        mobile = self._validate_mobile(mobile)
        if not captcha.strip():
            raise KetangpaiError("图形验证码不能为空")
        payload = self._post_json(
            "/UserApi/sendCode",
            {
                "mobile": mobile,
                "type": "login",
                "verify": captcha.strip(),
                "sessionid": session_id,
            },
        )
        if int(payload.get("status") or 0) != 1:
            raise KetangpaiError(_response_message(payload, "课堂派短信验证码发送失败"))
        return _response_message(payload, "短信验证码已发送")

    def verify_session(self, *, auto_relogin: bool = True) -> KetangpaiSessionStatus:
        credentials = self.store.load()
        if credentials is None:
            self._last_status = KetangpaiSessionStatus(
                False, False, "", "", "尚未登录课堂派"
            )
            return self._last_status
        try:
            response = self._post_json(
                "/UserApi/getUserBasinInfo",
                {"reqtimestamp": _milliseconds()},
                token=credentials.token,
            )
            data = response.get("data") if isinstance(response.get("data"), dict) else {}
            refreshed_token = str(data.get("token") or "").strip()
            valid = int(response.get("status") or 0) == 1 and bool(
                refreshed_token or data.get("uid")
            )
            if valid:
                if refreshed_token and refreshed_token != credentials.token:
                    self.store.save(
                        credentials.account,
                        refreshed_token,
                        password=credentials.password,
                        login_method=credentials.login_method,
                    )
                self._last_status = KetangpaiSessionStatus(
                    True,
                    True,
                    credentials.account,
                    credentials.login_method,
                    "课堂派登录会话有效",
                )
                return self._last_status
        except KetangpaiError:
            valid = False

        if auto_relogin and credentials.password:
            try:
                return self.login_password(credentials.account, credentials.password)
            except KetangpaiError:
                pass
        self._last_status = KetangpaiSessionStatus(
            True,
            False,
            credentials.account,
            credentials.login_method,
            "课堂派会话已失效，请重新登录",
        )
        return self._last_status

    def submit_scan(self, raw_url: str) -> dict[str, object]:
        fields = _parse_scan_fields(raw_url)
        try:
            credentials = self.store.load()
        except CredentialStoreError as exc:
            raise KetangpaiAuthenticationError(str(exc)) from exc
        if credentials is None:
            raise KetangpaiAuthenticationError("请先在“账户与认证”中登录课堂派")
        for attempt in range(2):
            response = self._post_json(
                "/AttenceApi/AttenceResult",
                {**fields, "reqtimestamp": int(time.time())},
                token=credentials.token,
            )
            if not _is_auth_failure(response) or attempt > 0 or not credentials.password:
                return response
            self.login_password(credentials.account, credentials.password)
            credentials = self.store.load() or credentials
        return response

    def clear_session(self) -> None:
        self.store.clear()
        self._last_status = None

    def _login(self, account: str, *, password: str = "", code: str = "", endpoint: str) -> str:
        payload = self._post_json(
            endpoint,
            {
                "mobile": account,
                "password": _encrypt_password(password),
                "encryption": "1",
                "reqtimestamp": _milliseconds(),
                "email": account,
                "type": "login",
                "remember": "0",
                "code": code,
            },
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        token = str(data.get("token") or "").strip()
        if int(payload.get("status") or 0) != 1 or not token:
            raise KetangpaiAuthenticationError(_response_message(payload, "课堂派登录失败"))
        return token

    def _post_json(
        self,
        path: str,
        body: dict[str, object],
        *,
        token: str = "",
    ) -> dict[str, object]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://www.ketangpai.com",
            "Referer": "https://www.ketangpai.com/",
            "User-Agent": "FuckClassroom/1.0",
        }
        if token:
            headers["token"] = token
        request = Request(
            f"{KETANGPAI_BASE_URL}{path}",
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = int(getattr(response, "status", 200))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status = exc.code
        except URLError as exc:
            raise KetangpaiError(f"课堂派请求失败：{exc.reason}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KetangpaiError(f"课堂派接口返回异常（HTTP {status}）") from exc
        if not isinstance(payload, dict):
            raise KetangpaiError("课堂派接口返回异常")
        payload["_http_status"] = status
        return payload

    @staticmethod
    def _validate_mobile(mobile: str) -> str:
        mobile = mobile.strip()
        if not _PHONE_RE.fullmatch(mobile):
            raise KetangpaiError("请输入 11 位课堂派登录手机号")
        return mobile


def _encrypt_password(password: str) -> str:
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(password.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_LOGIN_KEY), modes.CBC(_LOGIN_KEY)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def _parse_scan_fields(raw_url: str) -> dict[str, str]:
    parsed = urlparse(raw_url.strip())
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        hostname == "ketangpai.com" or hostname.endswith(".ketangpai.com")
    ):
        raise KetangpaiError("不是有效的课堂派签到二维码")
    query = parse_qs(parsed.query, keep_blank_values=True)
    fields = {key: str((query.get(key) or [""])[0]).strip() for key in ("ticketid", "expire", "sign")}
    if not all(fields.values()):
        raise KetangpaiError("课堂派二维码签到参数不完整")
    return fields


def _response_message(payload: dict[str, object], fallback: str) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for value in (data.get("info"), payload.get("message"), payload.get("msg")):
        if value:
            return str(value)
    return fallback


def _is_auth_failure(payload: dict[str, object]) -> bool:
    if payload.get("_http_status") in {401, 403}:
        return True
    message = _response_message(payload, "").lower()
    return any(marker in message for marker in ("token", "登录", "登陆", "过期", "失效"))


def _milliseconds() -> int:
    return int(time.time() * 1000)
