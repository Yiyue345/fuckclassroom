from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from fuckclassroom.auth.webvpn import (
    WebVpnSessionError, WebVpnSessionService, _classify_gateway_page,
    read_storage_state, safe_history_url, save_storage_state, validate_webvpn_url,
)
from fuckclassroom.core.config import AppConfig


class WebVpnBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = AppConfig(data_dir=Path(self.tmp.name))
        self.store = Mock()
        self.store.load.return_value = object()
        self.service = WebVpnSessionService(
            self.config, credential_store=self.store,
            verification_broker=Mock(), login_lock=threading.RLock(), cooldown=3,
        )
        save_storage_state(self.service.storage_path, {
            "cookies": [{"name": "webvpn", "domain": ".v.guet.edu.cn",
                         "path": "/", "value": "redacted"}], "origins": [],
        })

    def test_gateway_login_not_academic_home(self):
        page = Mock(url="https://v.guet.edu.cn/login?local_login=true")
        self.assertEqual(_classify_gateway_page(page, None), "expired")
        page.url = "https://cas.guet.edu.cn/authserver/login"
        self.assertEqual(_classify_gateway_page(page, None), "expired")
        page.url = "https://v.guet.edu.cn/"
        page.evaluate.return_value = False
        page.locator.return_value.count.return_value = 0
        self.assertEqual(_classify_gateway_page(page, Mock(status=403)), "unknown")
        self.assertEqual(_classify_gateway_page(page, Mock(status=200)), "unknown")
        page.evaluate.return_value = True
        self.assertEqual(_classify_gateway_page(page, Mock(status=200)), "authenticated")

    def test_storage_merge_preserves_unrelated_cookies(self):
        save_storage_state(self.service.storage_path, {"cookies": [
            {"name": "jw", "domain": "bkjwtest.guet.edu.cn", "path": "/", "value": "1"},
        ], "origins": []})
        data = read_storage_state(self.service.storage_path)
        self.assertEqual({c["name"] for c in data["cookies"]}, {"webvpn", "jw"})

    def test_atomic_merge_across_processes(self):
        script = """
import sys
from pathlib import Path
from fuckclassroom.auth.webvpn import save_storage_state
path = Path(sys.argv[1])
name = sys.argv[2]
for i in range(30):
    save_storage_state(path, {"cookies": [
        {"name": name, "domain": ".v.guet.edu.cn", "path": "/", "value": str(i)}
    ], "origins": []})
"""
        processes = [
            subprocess.Popen([sys.executable, "-c", script,
                              str(self.service.storage_path), name])
            for name in ("p1", "p2")
        ]
        for process in processes:
            self.assertEqual(process.wait(timeout=20), 0)
        state = read_storage_state(self.service.storage_path)
        self.assertEqual(
            {cookie["name"] for cookie in state["cookies"]},
            {"webvpn", "p1", "p2"},
        )

    def test_expired_parallel_requests_single_flight(self):
        calls = []
        self.service._probe = Mock(return_value=self.service._set("expired", "expired"))
        def login(*args, **kwargs):
            calls.append(True)
            time.sleep(0.1)
            self.service._last_verified = time.monotonic()
            return self.service._set("authenticated", "ready", checked=True)
        self.service._login = login
        outputs = []
        threads = [threading.Thread(target=lambda: outputs.append(
            self.service.ensure_authenticated(False))) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(s.is_authenticated for s in outputs))

    def test_failure_cooldown_and_explicit_reconnect_after_logout(self):
        self.service._probe = Mock(return_value=self.service._set("expired", "expired"))
        self.service._login = Mock(return_value=self.service._set("failed", "failed"))
        self.service.ensure_authenticated(False)
        self.service.ensure_authenticated(False)
        self.assertEqual(self.service._login.call_count, 1)
        self.service.clear_session()
        self.assertEqual(self.service.get_status().state, "logged_out")
        self.service.ensure_authenticated(False)
        self.assertEqual(self.service._login.call_count, 1)
        self.assertFalse(self.service.storage_path.exists())
        self.service._probe = Mock(return_value=self.service._set("login_required", "no cookie"))
        self.service.reconnect(interactive_allowed=False)
        self.assertNotEqual(self.service.get_status().state, "logged_out")

    def test_history_and_url_origin_sanitization(self):
        self.assertEqual(
            safe_history_url("https://v.guet.edu.cn/https/777/path?ticket=secret#token"),
            "https://v.guet.edu.cn/https/777/path",
        )
        for url in ("http://v.guet.edu.cn/", "https://127.0.0.1/",
                    "https://evil.example/", "https://v.guet.edu.cn.evil.com/"):
            with self.assertRaises(WebVpnSessionError):
                validate_webvpn_url(url)
        with self.assertRaises(WebVpnSessionError):
            safe_history_url("https://v.guet.edu.cn/login?ticket=secret")
        self.assertEqual(validate_webvpn_url(
            "https://iw.guet.edu.cn/", allowed_origins=("https://iw.guet.edu.cn",)),
            "https://iw.guet.edu.cn/")
        with self.assertRaises(WebVpnSessionError):
            validate_webvpn_url("https://iw.guet.edu.cn/")

    def test_status_contains_no_sensitive_cookie(self):
        result = self.service.get_status()
        self.assertNotIn("redacted", repr(result))


if __name__ == "__main__":
    unittest.main()
