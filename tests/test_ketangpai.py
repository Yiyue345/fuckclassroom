from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from fuckclassroom.plugins.attendance.ketangpai import (
    KetangpaiAuthenticationError,
    KetangpaiClient,
    KetangpaiCredentialStore,
    _encrypt_password,
)


def _protect(data: bytes) -> bytes:
    return b"test:" + data[::-1]


def _unprotect(data: bytes) -> bytes:
    if not data.startswith(b"test:"):
        raise ValueError("invalid")
    return data.removeprefix(b"test:")[::-1]


class _Headers:
    def get_content_type(self) -> str:
        return "application/json"


class _Response:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status
        self.headers = _Headers()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args) -> bytes:
        return self.payload


class KetangpaiTests(unittest.TestCase):
    def test_password_encryption_matches_aes_cbc_protocol(self) -> None:
        encrypted = base64.b64decode(_encrypt_password("secret-password"))
        key = b"ktp4567890123456"
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()

        self.assertEqual(
            unpadder.update(padded) + unpadder.finalize(),
            b"secret-password",
        )

    def test_credentials_and_token_are_encrypted_at_rest(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "ketangpai.json"
            store = KetangpaiCredentialStore(path, protect=_protect, unprotect=_unprotect)
            store.save("student@example.com", "token-value", password="secret")

            raw = path.read_text(encoding="utf-8")
            loaded = store.load()

            self.assertNotIn("student@example.com", raw)
            self.assertNotIn("token-value", raw)
            self.assertNotIn("secret", raw)
            self.assertEqual(loaded.account, "student@example.com")
            self.assertEqual(loaded.token, "token-value")

    def test_password_login_saves_official_token_and_expected_body(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = KetangpaiCredentialStore(
                Path(directory) / "ketangpai.json",
                protect=_protect,
                unprotect=_unprotect,
            )
            client = KetangpaiClient(store)
            captured = {}

            def fake_open(request, timeout):
                captured["url"] = request.full_url
                captured["body"] = json.loads(request.data)
                self.assertEqual(timeout, 30)
                return _Response({"status": 1, "data": {"token": "session-token"}})

            with patch("fuckclassroom.plugins.attendance.ketangpai.urlopen", side_effect=fake_open):
                status = client.login_password("student-id", "secret-password")

            self.assertTrue(status.is_valid)
            self.assertTrue(captured["url"].endswith("/UserApi/login"))
            self.assertEqual(captured["body"]["mobile"], "student-id")
            self.assertEqual(captured["body"]["email"], "student-id")
            self.assertEqual(captured["body"]["encryption"], "1")
            self.assertNotEqual(captured["body"]["password"], "secret-password")
            self.assertEqual(store.load().token, "session-token")

    def test_qr_sign_uses_saved_token_and_official_fields(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = KetangpaiCredentialStore(
                Path(directory) / "ketangpai.json",
                protect=_protect,
                unprotect=_unprotect,
            )
            store.save("student-id", "session-token", password="secret")
            client = KetangpaiClient(store)
            captured = {}

            def fake_open(request, timeout):
                captured["url"] = request.full_url
                captured["body"] = json.loads(request.data)
                captured["token"] = request.headers.get("Token")
                return _Response({"status": 1, "data": {"state": 8, "info": "签到成功"}})

            with patch("fuckclassroom.plugins.attendance.ketangpai.urlopen", side_effect=fake_open):
                result = client.submit_scan(
                    "https://www.ketangpai.com/sign/scan?ticketid=t1&expire=e1&sign=s1"
                )

            self.assertTrue(captured["url"].endswith("/AttenceApi/AttenceResult"))
            self.assertEqual(captured["token"], "session-token")
            self.assertEqual(captured["body"]["ticketid"], "t1")
            self.assertEqual(captured["body"]["expire"], "e1")
            self.assertEqual(captured["body"]["sign"], "s1")
            self.assertEqual(result["data"]["state"], 8)

    def test_qr_sign_requires_separate_ketangpai_login(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            client = KetangpaiClient(
                KetangpaiCredentialStore(
                    Path(directory) / "ketangpai.json",
                    protect=_protect,
                    unprotect=_unprotect,
                )
            )

            with self.assertRaises(KetangpaiAuthenticationError):
                client.submit_scan(
                    "https://www.ketangpai.com/sign/scan?ticketid=t1&expire=e1&sign=s1"
                )


if __name__ == "__main__":
    unittest.main()
