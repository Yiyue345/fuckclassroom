from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from starlette.requests import Request

from fuckclassroom.auth.credentials import SelectionCredentials
from fuckclassroom.core.config import AppConfig
from fuckclassroom.web import create_app


class WebVpnRouteEventLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_clear_keeps_event_loop_responsive_during_login_lock_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(AppConfig(data_dir=Path(directory)))
            broker = app.state.plugin_runtime.services.get("webvpn_session")
            entered = threading.Event()
            release = threading.Event()
            original = broker.clear_session

            def blocked():
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test clear was never released")
                original()

            broker.clear_session = blocked
            route = next(r for r in app.routes if getattr(r, "path", "") == "/settings/login-state/clear")
            task = asyncio.create_task(route.endpoint())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                # This coroutine must run BEFORE releasing the worker thread.
                await asyncio.wait_for(asyncio.sleep(.02), timeout=.3)
                self.assertFalse(task.done())
            finally:
                release.set()
            response = await asyncio.wait_for(task, timeout=5)
            self.assertEqual(response.status_code, 303)
            self.assertIn("login_state_cleared=1", response.headers["location"])

    async def test_account_switch_does_not_block_event_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(AppConfig(data_dir=Path(directory)))
            services = app.state.plugin_runtime.services
            credential_store = services.get("credential_store")
            credential_store.load = lambda: SelectionCredentials("first", "password-a")
            credential_store.update_from_form = lambda username, password: SelectionCredentials(
                "second", "password-b"
            )
            broker = services.get("webvpn_session")
            entered = threading.Event()
            release = threading.Event()
            original = broker.clear_session

            def blocked():
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test switch was never released")
                original()

            broker.clear_session = blocked
            async def receive():
                return {"type": "http.request", "body": b"account_username=second", "more_body": False}

            request = Request(
                {"type": "http", "method": "POST", "path": "/settings", "headers": []},
                receive,
            )
            route = next(r for r in app.routes if getattr(r, "path", "") == "/settings"
                         and "POST" in getattr(r, "methods", set()))
            task = asyncio.create_task(route.endpoint(request))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                await asyncio.wait_for(asyncio.sleep(.02), timeout=.3)
                self.assertFalse(task.done())
            finally:
                release.set()
            response = await asyncio.wait_for(task, timeout=5)
            self.assertEqual(response.status_code, 303)
            self.assertIn("saved=1", response.headers["location"])

    async def test_account_switch_timeout_rolls_back_without_late_logout(self):
        from unittest.mock import patch
        from fuckclassroom.auth.webvpn_storage import (
            begin_session, session_generation, save_storage_state,
        )

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(AppConfig(data_dir=Path(directory)))
            services = app.state.plugin_runtime.services
            broker = services.get("webvpn_session")
            credential_store = services.get("credential_store")
            credential_store.load = lambda: SelectionCredentials("previous", "password-old")
            credential_store.update_from_form = (
                lambda username, password: SelectionCredentials("next", "password-new")
            )
            save_storage_state(broker.storage_path, {"cookies": [
                {"name": "auth", "domain": ".v.guet.edu.cn", "path": "/", "value": "session"}
            ], "origins": []})
            initial = session_generation(broker.storage_path)
            login_held = threading.Event()
            release = threading.Event()

            def hold_login():
                with broker._login_lock:
                    login_held.set()
                    release.wait(timeout=5)

            worker = threading.Thread(target=hold_login)
            worker.start()
            self.assertTrue(await asyncio.to_thread(login_held.wait, 2))
            original_clear = broker.clear_session
            broker.clear_session = lambda: original_clear(timeout_seconds=0.12)

            async def receive():
                return {"type": "http.request", "body": b"account_username=next", "more_body": False}

            request = Request(
                {"type": "http", "method": "POST", "path": "/settings", "headers": []},
                receive,
            )
            route = next(r for r in app.routes if getattr(r, "path", "") == "/settings"
                         and "POST" in getattr(r, "methods", set()))
            try:
                response = await asyncio.wait_for(route.endpoint(request), timeout=2)
                self.assertEqual(response.status_code, 303)
                self.assertIn("settings_error=", response.headers["location"])
                self.assertEqual(session_generation(broker.storage_path), initial)
                self.assertTrue(broker.storage_path.exists())
            finally:
                release.set()
                worker.join(timeout=3)

            begin_session(broker.storage_path)
            rotated = session_generation(broker.storage_path)
            await asyncio.sleep(.15)
            self.assertEqual(session_generation(broker.storage_path), rotated)
            self.assertTrue(broker.storage_path.exists())

    async def test_clear_session_timeout_does_not_later_clear_new_session(self):
        from fuckclassroom.auth.webvpn_storage import (
            begin_session, save_storage_state, session_generation,
        )
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(AppConfig(data_dir=Path(directory)))
            broker = app.state.plugin_runtime.services.get("webvpn_session")
            save_storage_state(broker.storage_path, {"cookies": [
                {"name": "auth", "domain": ".v.guet.edu.cn", "path": "/", "value": "1"}
            ], "origins": []})
            entered = threading.Event()
            unlock = threading.Event()

            def hold():
                with broker._login_lock:
                    entered.set()
                    unlock.wait(timeout=4)

            thread = threading.Thread(target=hold)
            thread.start()
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            original = broker.clear_session
            broker.clear_session = lambda: original(timeout_seconds=0.1)
            route = next(r for r in app.routes if getattr(r, "path", "") == "/settings/login-state/clear")
            try:
                response = await asyncio.wait_for(route.endpoint(), timeout=2)
                self.assertIn("login_state_error=", response.headers["location"])
                self.assertTrue(broker.storage_path.exists())
            finally:
                unlock.set()
                thread.join(timeout=3)
            begin_session(broker.storage_path)
            current = session_generation(broker.storage_path)
            await asyncio.sleep(.12)
            self.assertEqual(session_generation(broker.storage_path), current)
            self.assertTrue(broker.storage_path.exists())

    async def test_manual_verification_is_cancelled_before_waiting_for_login_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(AppConfig(data_dir=Path(directory)))
            services = app.state.plugin_runtime.services
            verifier = services.get("verification_broker")
            challenge_id = verifier.begin("WebVPN")
            broker = services.get("webvpn_session")
            original = broker.clear_session
            events = []

            def clear():
                events.append(verifier.current())
                return original()

            broker.clear_session = clear
            route = next(r for r in app.routes if getattr(r, "path", "") == "/settings/login-state/clear")
            response = await asyncio.wait_for(route.endpoint(), timeout=5)
            self.assertEqual(response.status_code, 303)
            self.assertIsNotNone(events[0])
            self.assertEqual(events[0]["status"], "cancelled")
            verifier.finish(challenge_id)


if __name__ == "__main__":
    unittest.main()
