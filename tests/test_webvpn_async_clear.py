from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

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
