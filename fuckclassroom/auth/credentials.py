from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class CredentialStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class SelectionCredentials:
    username: str
    password: str


class SelectionCredentialStore:
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

    def load(self) -> SelectionCredentials | None:
        if not self.path.exists():
            return None
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            protected = base64.b64decode(str(envelope["protected"]), validate=True)
            payload = json.loads(self._unprotect(protected).decode("utf-8"))
            username = str(payload.get("username") or "").strip()
            password = str(payload.get("password") or "")
        except (OSError, KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError("统一桂电登录凭据无法读取，请重新保存") from exc
        if not username or not password:
            raise CredentialStoreError("统一桂电登录凭据不完整，请重新保存")
        return SelectionCredentials(username=username, password=password)

    def save(self, username: str, password: str) -> SelectionCredentials:
        normalized_username = username.strip()
        if not normalized_username or not password:
            raise CredentialStoreError("统一桂电账号和密码不能为空")
        credentials = SelectionCredentials(normalized_username, password)
        plaintext = json.dumps(
            {"username": credentials.username, "password": credentials.password},
            ensure_ascii=False,
        ).encode("utf-8")
        protected = self._protect(plaintext)
        envelope = {
            "version": 1,
            "protected": base64.b64encode(protected).decode("ascii"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise CredentialStoreError(f"统一桂电登录凭据保存失败：{exc}") from exc
        return credentials

    def update_from_form(self, username: str, password: str) -> SelectionCredentials | None:
        normalized_username = username.strip()
        if normalized_username and password:
            return self.save(normalized_username, password)

        current = self.load()
        resolved_username = normalized_username or (current.username if current else "")
        resolved_password = password or (current.password if current else "")
        if not resolved_username and not resolved_password:
            return current
        return self.save(resolved_username, resolved_password)

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise CredentialStoreError(f"统一桂电登录凭据删除失败：{exc}") from exc

    def public_status(self) -> dict[str, object]:
        try:
            credentials = self.load()
        except CredentialStoreError as exc:
            return {
                "is_saved": False,
                "can_clear": self.path.exists(),
                "username": "",
                "message": str(exc),
            }
        if credentials is None:
            return {
                "is_saved": False,
                "can_clear": False,
                "username": "",
                "message": "尚未保存统一桂电账号",
            }
        return {
            "is_saved": True,
            "can_clear": True,
            "username": credentials.username,
            "message": "录播与教务会话失效时将自动登录",
        }


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _protect_with_dpapi(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialStoreError("当前系统不支持 Windows DPAPI 凭据存储")
    return _call_dpapi("CryptProtectData", data)


def _unprotect_with_dpapi(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialStoreError("当前系统不支持 Windows DPAPI 凭据存储")
    return _call_dpapi("CryptUnprotectData", data)


def _call_dpapi(function_name: str, data: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = getattr(crypt32, function_name)
    function.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR if function_name == "CryptProtectData" else ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    buffer = ctypes.create_string_buffer(data, len(data))
    input_blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = _DataBlob()
    description = "Fuck Classroom 统一桂电登录凭据" if function_name == "CryptProtectData" else None
    description_pointer = None if description is None else description
    if not function(
        ctypes.byref(input_blob),
        description_pointer,
        None,
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    ):
        raise CredentialStoreError(f"Windows DPAPI 操作失败：{ctypes.WinError(ctypes.get_last_error())}")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))
