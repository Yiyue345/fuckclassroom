"""Actual Registry ZIP / host-PR composition test, no real GUET credentials.

Run with COURSE_SELECTION_ZIP and LAB_SELECTION_ZIP set to separately built
external-plugin release-format ZIPs from their corresponding repository refs.
"""
from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fuckclassroom.auth.webvpn_storage import (
    begin_session, read_session_snapshot, read_storage_state, save_storage_state,
)
from fuckclassroom.core.config import AppConfig
from fuckclassroom.plugins.manager import PluginManager
from fuckclassroom.web import create_app


class _Context:
    def __init__(self, state):
        self.state = copy.deepcopy(state or {"cookies": [], "origins": []})

    def storage_state(self):
        return copy.deepcopy(self.state)


class _Browser:
    def new_context(self, *, storage_state=None):
        return _Context(storage_state)


@unittest.skipUnless(
    os.environ.get("COURSE_SELECTION_ZIP") and os.environ.get("LAB_SELECTION_ZIP"),
    "Use the cross-repository ZIP integration workflow",
)
class RealPluginZipIntegrationTests(unittest.TestCase):
    def test_registry_zips_install_enable_and_share_gateway_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(data_dir=Path(directory))
            manager = PluginManager(config)
            course = manager.install_from_path(os.environ["COURSE_SELECTION_ZIP"])
            lab = manager.install_from_path(os.environ["LAB_SELECTION_ZIP"])
            self.assertEqual((course.id, lab.id), ("course_selection", "lab_selection"))
            desired = manager.enable("lab_selection")
            self.assertIn("course_selection", desired)

            # Load real external modules from both installed ZIPs, not host's
            # dormant plugin implementation or synthetic legacy wrappers.
            app = create_app(config)
            services = app.state.plugin_runtime.services
            broker = services.get("webvpn_session")
            academic = services.get("academic_session")
            facade = services.get("course_selection")
            self.assertIs(facade.local, academic)
            self.assertIs(academic._webvpn_session, broker)
            self.assertIs(services.get("lab_selection").session, academic)
            self.assertIn("fuckclassroom_external_course_selection", type(academic).__module__)
            self.assertIn("/lab-selection", {r.path for r in app.routes})

            base = {"cookies": [
                {"name": "gateway", "domain": ".v.guet.edu.cn", "path": "/", "value": "first"}
            ], "origins": []}
            save_storage_state(broker.storage_path, base)
            context = academic._new_login_context(_Browser())
            context.state["cookies"][0]["value"] = "second"
            # Another thread/plugin updates an unrelated origin concurrently.
            fresh, generation = read_session_snapshot(broker.storage_path)
            save_storage_state(
                broker.storage_path, {"cookies": [
                    {"name": "other", "domain": "bkjwtest.guet.edu.cn", "path": "/", "value": "keep"}
                ], "origins": []},
                expected_generation=generation,
            )
            academic._save_browser_storage_state(context)
            cookies = {item["name"]: item["value"] for item in
                       read_storage_state(broker.storage_path)["cookies"]}
            self.assertEqual(cookies, {"gateway": "second", "other": "keep"})

            academic._selection_access = lambda: SimpleNamespace(mode="webvpn", label="WebVPN")
            academic._saved_access_mode = lambda: "webvpn"
            _opener, jar = academic._selection_http_client()
            for cookie in jar:
                if cookie.name == "gateway":
                    cookie.value = "third"
            academic._persist_selection_cookies(jar)
            cookies = {item["name"]: item["value"] for item in
                       read_storage_state(broker.storage_path)["cookies"]}
            self.assertEqual(cookies, {"gateway": "third", "other": "keep"})

            # Old worker HTTP saves must fail after an account change.
            _opener, jar = academic._selection_http_client()
            begin_session(broker.storage_path)
            with self.assertRaisesRegex(Exception, "会话已变化"):
                academic._persist_selection_cookies(jar)
            # Explicit login rotation clears old SSO cookies, preserving the
            # independently owned campus-cookie domain.
            self.assertEqual(
                {item["name"] for item in read_storage_state(broker.storage_path)["cookies"]},
                {"other"},
            )


if __name__ == "__main__":
    unittest.main()
