from __future__ import annotations

import subprocess
import sys
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fuckclassroom.auth.webvpn_storage import (
    WebVpnSessionError as GenerationError,
    begin_session, invalidate_session, new_browser_context, read_session_snapshot,
    session_generation, save_browser_context,
)
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

    def test_visible_cas_auto_login_uses_broker_credentials_only(self):
        page = Mock(url="https://cas.guet.edu.cn/authserver/login")
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.is_visible.return_value = True
        credentials = object()
        self.store.load.return_value = credentials
        with patch("fuckclassroom.auth.webvpn.is_sms_verification_page", return_value=False), \
             patch("fuckclassroom.auth.webvpn.login_interaction_reason", return_value=""), \
             patch("fuckclassroom.auth.webvpn.submit_saved_credentials", return_value=True) as submit:
            result = self.service.submit_saved_login(page)
        self.assertEqual(result, "submitted")
        self.store.load.assert_called()
        submit.assert_called_once_with(page, credentials)
        self.assertIsInstance(result, str)

    def test_saved_auto_login_rejects_untrusted_hosts_without_reading_secrets(self):
        self.store.load.reset_mock()
        page = Mock(url="https://attacker.example/login")
        self.assertEqual(self.service.submit_saved_login(page), "not_cas")
        page.url = "http://cas.guet.edu.cn/authserver/login"
        self.assertEqual(self.service.submit_saved_login(page), "not_cas")
        page.url = "https://v.guet.edu.cn/https/unsafe-app/login"
        self.assertEqual(self.service.submit_saved_login(page), "not_cas")
        self.store.load.assert_not_called()

    def test_saved_auto_login_respects_otp_logout_and_missing_credentials(self):
        page = Mock(url="https://cas.guet.edu.cn/authserver/login")
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.is_visible.return_value = True
        with patch("fuckclassroom.auth.webvpn.is_sms_verification_page", return_value=True):
            self.assertEqual(self.service.submit_saved_login(page), "verification_required")
        self.store.load.reset_mock()
        with patch("fuckclassroom.auth.webvpn.is_sms_verification_page", return_value=False), \
             patch("fuckclassroom.auth.webvpn.login_interaction_reason", return_value=""):
            self.store.load.return_value = None
            self.assertEqual(self.service.submit_saved_login(page), "no_credentials")
            self.service.clear_session()
            self.assertEqual(self.service.submit_saved_login(page), "logged_out")
        self.store.load.assert_called_once()

    def test_unready_login_form_never_reads_credentials(self):
        page = Mock(url="https://cas.guet.edu.cn/authserver/login")
        page.locator.return_value.count.return_value = 0
        with patch("fuckclassroom.auth.webvpn.is_sms_verification_page", return_value=False), \
             patch("fuckclassroom.auth.webvpn.login_interaction_reason", return_value=""):
            self.assertEqual(self.service.submit_saved_login(page), "not_ready")
        self.store.load.assert_not_called()

    def test_status_read_does_not_write_lockfile(self):
        self.service.storage_path.unlink()
        lockfile = self.service.storage_path.with_name(
            self.service.storage_path.name + ".lock"
        )
        lockfile.unlink(missing_ok=True)
        self.assertEqual(self.service.get_status().state, "login_required")
        self.assertFalse(lockfile.exists())

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

    def test_cross_process_account_rotation_invalidates_cached_auth_status(self):
        self.service._set("authenticated", "ready", checked=True)
        self.service._last_verified = time.monotonic()
        begin_session(self.service.storage_path)
        self.assertEqual(self.service.get_status().state, "unknown")
        self.service._probe = Mock(return_value=self.service._set("unknown", "revalidate"))
        self.service.ensure_authenticated(interactive_allowed=False)
        self.service._probe.assert_called_once()

    def test_generation_fences_stale_context_after_account_switch(self):
        class Context:
            def __init__(self, state):
                self.state = state
            def storage_state(self):
                return self.state

        class Browser:
            def new_context(self, storage_state=None):
                return Context(storage_state or {"cookies": [], "origins": []})

        browser = Browser()
        old_context = new_browser_context(browser, self.service.storage_path)
        invalidate_session(self.service.storage_path)
        self.assertTrue(session_generation(self.service.storage_path)[1])
        with self.assertRaises(GenerationError):
            save_browser_context(self.service.storage_path, old_context)
        begin_session(self.service.storage_path)
        save_storage_state(
            self.service.storage_path, {"cookies": [
                {"name": "new-account", "domain": ".v.guet.edu.cn", "value": "NEW", "path": "/"}
            ], "origins": []},
            expected_generation=session_generation(self.service.storage_path)[0],
        )
        with self.assertRaises(GenerationError):
            save_browser_context(self.service.storage_path, old_context)
        self.assertEqual(
            {x["name"] for x in read_storage_state(self.service.storage_path)["cookies"]},
            {"new-account"},
        )

    def test_same_account_renewal_preserves_generation_and_all_plugin_cookies(self):
        from fuckclassroom.auth.webvpn_storage import save_storage_state
        original, generation = read_session_snapshot(self.service.storage_path)
        browser = type("Browser", (), {"new_context": lambda self, storage_state=None:
            type("Context", (), {"storage_state": lambda self: storage_state})()})()
        old_context = new_browser_context(browser, self.service.storage_path)
        self.assertEqual(begin_session(self.service.storage_path, mode="renewal"), generation)
        self.assertEqual(read_session_snapshot(self.service.storage_path)[0], original)
        save_browser_context(self.service.storage_path, old_context)
        self.assertEqual(session_generation(self.service.storage_path), (generation, False))

    def test_explicit_relogin_purges_only_owned_cookies_and_origins(self):
        state = {
            "cookies": [
                {"name": "vpn", "domain": ".v.guet.edu.cn", "path": "/", "value": "old"},
                {"name": "cas", "domain": "cas.guet.edu.cn", "path": "/", "value": "old"},
                {"name": "jw", "domain": "bkjwtest.guet.edu.cn", "path": "/", "value": "old"},
                {"name": "domain", "domain": ".guet.edu.cn", "path": "/", "value": "shared-old"},
                {"name": "library", "domain": "library.guet.edu.cn", "path": "/", "value": "keep"},
                {"name": "external", "domain": "service.example.org", "path": "/", "value": "keep"},
            ],
            "origins": [
                {"origin": "https://v.guet.edu.cn", "localStorage": [{"name": "session", "value": "old"}]},
                {"origin": "https://bkjwtest.guet.edu.cn", "localStorage": [{"name": "token", "value": "old"}]},
                {"origin": "https://library.guet.edu.cn", "localStorage": [{"name": "keep", "value": "yes"}]},
                {"origin": "https://service.example.org", "localStorage": [{"name": "keep", "value": "yes"}]},
            ],
        }
        self.service.storage_path.write_text(json.dumps(state), encoding="utf-8")
        old, previous_generation = read_session_snapshot(self.service.storage_path)
        new_generation = begin_session(self.service.storage_path, mode="relogin")
        self.assertNotEqual(previous_generation, new_generation)
        after = read_storage_state(self.service.storage_path)
        self.assertEqual({c["name"] for c in after["cookies"]}, {"library", "external"})
        self.assertEqual({o["origin"] for o in after["origins"]},
                         {"https://library.guet.edu.cn", "https://service.example.org"})
        with self.assertRaises(GenerationError):
            save_storage_state(self.service.storage_path, old,
                               expected_generation=previous_generation, previous_state=old)

        class Context:
            def __init__(self):
                self.state = {"cookies": [], "origins": []}
            def storage_state(self):
                return self.state
        class Browser:
            def new_context(self, **kwargs):
                return Context()
        fresh = new_browser_context(Browser(), self.service.storage_path,
                                    reuse_saved_session=False)
        fresh.state["cookies"].append({
            "name": "vpn", "domain": ".v.guet.edu.cn", "path": "/", "value": "new"
        })
        save_browser_context(self.service.storage_path, fresh)
        final = {c["name"]: c["value"] for c in
                 read_storage_state(self.service.storage_path)["cookies"]}
        self.assertEqual(final, {"library": "keep", "external": "keep", "vpn": "new"})

    def test_account_switch_removes_all_campus_cookies_but_not_external_plugins(self):
        path = self.service.storage_path
        path.write_text(json.dumps({
            "cookies": [
                {"name": "a", "domain": ".v.guet.edu.cn", "value": "old", "path": "/"},
                {"name": "b", "domain": ".library.guet.edu.cn", "value": "old", "path": "/"},
                {"name": "c", "domain": ".guet.edu.cn", "value": "old", "path": "/"},
                {"name": "d", "domain": "service.example.org", "value": "safe", "path": "/"},
                {"name": "e", "domain": "guet.edu.cn.evil.test", "value": "safe", "path": "/"},
            ],
            "origins": [
                {"origin": "https://library.guet.edu.cn", "localStorage": [{"name": "x", "value": "a"}]},
                {"origin": "https://service.example.org", "localStorage": [{"name": "x", "value": "b"}]},
            ],
        }), encoding="utf-8")
        before = session_generation(path)[0]
        after = begin_session(path, mode="account_switch")
        self.assertNotEqual(before, after)
        saved = read_storage_state(path)
        self.assertEqual({c["name"] for c in saved["cookies"]}, {"d", "e"})
        self.assertEqual([o["origin"] for o in saved["origins"]], ["https://service.example.org"])

    def test_fresh_browser_without_begin_still_deletes_old_owned_cookie_only(self):
        path = self.service.storage_path
        path.write_text(json.dumps({
            "cookies": [
                {"name": "oldvpn", "domain": "v.guet.edu.cn", "path": "/", "value": "stale"},
                {"name": "oldcas", "domain": "cas.guet.edu.cn", "path": "/", "value": "stale"},
                {"name": "plugin", "domain": "library.guet.edu.cn", "path": "/", "value": "keep"},
            ],
            "origins": [
                {"origin": "https://v.guet.edu.cn", "localStorage": [{"name": "oldtoken", "value": "secret"}]},
                {"origin": "https://library.guet.edu.cn", "localStorage": [{"name": "keep", "value": "data"}]},
            ],
        }), encoding="utf-8")
        class Context:
            def __init__(self):
                self.state = {"cookies": [], "origins": []}
            def storage_state(self):
                return self.state
        class Browser:
            def new_context(self, **kwargs):
                return Context()
        context = new_browser_context(Browser(), path, reuse_saved_session=False)
        context.state["cookies"].append({
            "name": "fresh", "domain": ".v.guet.edu.cn", "path": "/", "value": "NEW"
        })
        save_browser_context(path, context)
        after = read_storage_state(path)
        self.assertEqual({c["name"] for c in after["cookies"]}, {"fresh", "plugin"})
        self.assertEqual([o["origin"] for o in after["origins"]], ["https://library.guet.edu.cn"])

    def test_renewal_cannot_implicitly_reconnect_after_logout(self):
        self.service.clear_session()
        with self.assertRaises(GenerationError):
            begin_session(self.service.storage_path, mode="renewal")
        self.assertTrue(session_generation(self.service.storage_path)[1])

    def test_browser_delta_deletes_removed_cookies_and_local_storage(self):
        initial = {"cookies": [
            {"name": "session_a", "domain": ".v.guet.edu.cn", "path": "/", "value": "old"},
            {"name": "keep", "domain": "bkjwtest.guet.edu.cn", "path": "/", "value": "other"},
        ], "origins": [
            {"origin": "https://v.guet.edu.cn", "localStorage": [
                {"name": "gone", "value": "old"}, {"name": "keep", "value": "same"}]},
            {"origin": "https://other.guet.edu.cn", "localStorage": [{"name": "unrelated", "value": "1"}]},
        ]}
        self.service.storage_path.write_text(json.dumps(initial), encoding="utf-8")
        before, generation = read_session_snapshot(self.service.storage_path)
        changed = {"cookies": [
            {"name": "keep", "domain": "bkjwtest.guet.edu.cn", "path": "/", "value": "other"},
            {"name": "expired", "domain": ".v.guet.edu.cn", "path": "/", "value": "x", "expires": 1},
        ], "origins": [
            {"origin": "https://v.guet.edu.cn", "localStorage": [{"name": "keep", "value": "same"}]},
            {"origin": "https://other.guet.edu.cn", "localStorage": [{"name": "unrelated", "value": "1"}]},
        ]}
        save_storage_state(
            self.service.storage_path, changed,
            expected_generation=generation, previous_state=before,
        )
        current = read_storage_state(self.service.storage_path)
        self.assertEqual([x["name"] for x in current["cookies"]], ["keep"])
        local = {item["origin"]: {x["name"]: x["value"] for x in item["localStorage"]}
                 for item in current["origins"]}
        self.assertEqual(local["https://v.guet.edu.cn"], {"keep": "same"})
        self.assertEqual(local["https://other.guet.edu.cn"], {"unrelated": "1"})

    def test_timeout_with_login_lock_never_performs_late_logout(self):
        holding = threading.Event()
        release = threading.Event()

        def login_in_progress():
            with self.service._login_lock:
                holding.set()
                release.wait(timeout=4)

        busy = threading.Thread(target=login_in_progress)
        busy.start()
        try:
            self.assertTrue(holding.wait(timeout=1))
            before_generation = session_generation(self.service.storage_path)
            with self.assertRaisesRegex(TimeoutError, "超时"):
                self.service.clear_session(timeout_seconds=0.12)
            self.assertEqual(session_generation(self.service.storage_path), before_generation)
            self.assertTrue(self.service.storage_path.exists())
        finally:
            release.set()
            busy.join(timeout=2)
        # A fresh user connection deliberately clears the old SSO Cookie;
        # the timed-out logout must not invalidate the replacement session.
        new_generation = begin_session(self.service.storage_path, mode="relogin")
        save_storage_state(self.service.storage_path, {"cookies": [
            {"name": "replacement", "domain": ".v.guet.edu.cn",
             "path": "/", "value": "new"}
        ], "origins": []}, expected_generation=new_generation)
        after_generation = session_generation(self.service.storage_path)
        time.sleep(0.08)
        self.assertEqual(session_generation(self.service.storage_path), after_generation)
        self.assertEqual(read_storage_state(self.service.storage_path)["cookies"][0]["name"],
                         "replacement")

    def test_timeout_with_storage_file_lock_leaves_session_untouched(self):
        import multiprocessing
        from fuckclassroom.auth.webvpn_storage import _locked_storage_file

        with _locked_storage_file(self.service.storage_path):
            # The OS file lock is per process; a different process must hold it
            # for this boundary to be meaningful. Use a child that holds it.
            pass
        ready = Path(self.tmp.name) / "ready.lock"
        release = Path(self.tmp.name) / "release.lock"
        code = """
import sys,time
from pathlib import Path
from fuckclassroom.auth.webvpn_storage import _locked_storage_file
storage,ready,release = map(Path, sys.argv[1:])
with _locked_storage_file(storage):
    ready.touch()
    while not release.exists():
        time.sleep(.025)
"""
        proc = subprocess.Popen([sys.executable,"-c",code,str(self.service.storage_path),str(ready),str(release)])
        try:
            deadline=time.monotonic()+3
            while not ready.exists() and time.monotonic()<deadline:
                time.sleep(.025)
            self.assertTrue(ready.exists())
            with self.assertRaises(TimeoutError):
                self.service.clear_session(timeout_seconds=0.15)
            self.assertTrue(self.service.storage_path.exists())
            self.assertFalse(session_generation(self.service.storage_path)[1])
        finally:
            release.touch()
            self.assertEqual(proc.wait(timeout=5),0)

    def test_cross_process_old_http_snapshot_must_not_write_after_switch(self):
        ready = Path(self.tmp.name) / "old-http-ready"
        proceed = Path(self.tmp.name) / "old-http-proceed"
        code = """
import sys,time
from pathlib import Path
from fuckclassroom.auth.webvpn_storage import (
    read_session_snapshot, save_storage_state, WebVpnSessionError
)
path, ready, proceed = map(Path, sys.argv[1:])
previous, generation = read_session_snapshot(path)
ready.touch()
deadline = time.monotonic() + 8
while not proceed.exists() and time.monotonic() < deadline:
    time.sleep(.025)
try:
    save_storage_state(path, {"cookies": [
        {"name": "gateway", "domain": ".v.guet.edu.cn",
         "path": "/", "value": "stale-worker"}
    ], "origins": []}, expected_generation=generation, previous_state=previous)
except WebVpnSessionError:
    sys.exit(0)
sys.exit(3)
"""
        worker = subprocess.Popen([
            sys.executable, "-c", code, str(self.service.storage_path),
            str(ready), str(proceed),
        ])
        try:
            deadline = time.monotonic() + 4
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.025)
            self.assertTrue(ready.exists(), "HTTP worker failed to read old generation")
            invalidate_session(self.service.storage_path)
            new_gen = begin_session(self.service.storage_path)
            save_storage_state(self.service.storage_path, {"cookies": [
                {"name": "gateway", "domain": ".v.guet.edu.cn",
                 "path": "/", "value": "new-user"}
            ], "origins": []}, expected_generation=new_gen)
            proceed.touch()
            self.assertEqual(worker.wait(timeout=8), 0)
            cookies = read_storage_state(self.service.storage_path)["cookies"]
            self.assertEqual(len(cookies), 1)
            self.assertEqual(cookies[0]["value"], "new-user")
        finally:
            proceed.touch()
            if worker.poll() is None:
                worker.kill()
                worker.wait()

    def test_process_b_cannot_resurrect_a_context_after_logout(self):
        signal_path = Path(self.tmp.name) / "start"
        ready_path = Path(self.tmp.name) / "ready"
        script = """
import sys, time
from pathlib import Path
from fuckclassroom.auth.webvpn_storage import (
    new_browser_context, save_browser_context, WebVpnSessionError
)
class FakeContext:
    def __init__(self, data): self.data = data
    def storage_state(self): return self.data
class FakeBrowser:
    def new_context(self, storage_state=None):
        return FakeContext(storage_state or {"cookies": [], "origins": []})
path, ready, go = map(Path, sys.argv[1:])
ctx = new_browser_context(FakeBrowser(), path)
ready.touch()
deadline = time.time() + 15
while not go.exists() and time.time() < deadline:
    time.sleep(.03)
try:
    save_browser_context(path, ctx)
except WebVpnSessionError:
    sys.exit(0)
sys.exit(2)
"""
        proc = subprocess.Popen([
            sys.executable, "-c", script, str(self.service.storage_path),
            str(ready_path), str(signal_path),
        ])
        try:
            deadline = time.monotonic() + 12
            while not ready_path.exists() and time.monotonic() < deadline:
                time.sleep(.03)
            self.assertTrue(ready_path.exists(), "子进程没有创建旧浏览器 Context")
            self.service.clear_session()
            self.service.authorize_login()
            signal_path.touch()
            self.assertEqual(proc.wait(timeout=20), 0)
            self.assertFalse(self.service.storage_path.exists())
        finally:
            signal_path.touch()
            if proc.poll() is None:
                proc.kill()
                proc.wait()

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
