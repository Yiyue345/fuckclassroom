from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fuckclassroom.auth.credentials import CredentialStoreError, SelectionCredentialStore


def _protect(data: bytes) -> bytes:
    return b"test:" + data[::-1]


def _unprotect(data: bytes) -> bytes:
    if not data.startswith(b"test:"):
        raise ValueError("invalid test payload")
    return data.removeprefix(b"test:")[::-1]


class SelectionCredentialStoreTests(unittest.TestCase):
    def test_roundtrip_does_not_write_plaintext(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "credentials.json"
            store = SelectionCredentialStore(path, protect=_protect, unprotect=_unprotect)
            store.save("student-id", "secret-password")

            loaded = store.load()

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.username, "student-id")
            self.assertEqual(loaded.password, "secret-password")
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("student-id", raw)
            self.assertNotIn("secret-password", raw)

    def test_blank_password_preserves_saved_password(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = SelectionCredentialStore(
                Path(directory) / "credentials.json",
                protect=_protect,
                unprotect=_unprotect,
            )
            store.save("old-user", "saved-password")

            store.update_from_form("new-user", "")

            loaded = store.load()
            self.assertEqual(loaded.username, "new-user")
            self.assertEqual(loaded.password, "saved-password")

    def test_incomplete_new_credentials_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = SelectionCredentialStore(
                Path(directory) / "credentials.json",
                protect=_protect,
                unprotect=_unprotect,
            )

            with self.assertRaises(CredentialStoreError):
                store.update_from_form("student-id", "")

    def test_clear_removes_saved_credentials(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = SelectionCredentialStore(
                Path(directory) / "credentials.json",
                protect=_protect,
                unprotect=_unprotect,
            )
            store.save("student-id", "secret-password")

            store.clear()

            self.assertIsNone(store.load())
            self.assertFalse(store.public_status()["is_saved"])


    def test_complete_new_credentials_overwrite_corrupted_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "credentials.json"
            path.write_text("{broken-json", encoding="utf-8")
            store = SelectionCredentialStore(
                path,
                protect=_protect,
                unprotect=_unprotect,
            )

            saved = store.update_from_form("new-user", "new-password")

            self.assertIsNotNone(saved)
            loaded = store.load()
            self.assertEqual(loaded.username, "new-user")
            self.assertEqual(loaded.password, "new-password")

    def test_corrupted_file_status_remains_clearable(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "credentials.json"
            path.write_text("{broken-json", encoding="utf-8")
            store = SelectionCredentialStore(
                path,
                protect=_protect,
                unprotect=_unprotect,
            )

            status = store.public_status()

            self.assertFalse(status["is_saved"])
            self.assertTrue(status["can_clear"])
            self.assertIn("无法读取", str(status["message"]))


if __name__ == "__main__":
    unittest.main()
