"""Regression coverage for online-plugin card status and theme contrast."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from fuckclassroom.core.config import AppConfig
from fuckclassroom.plugins.registry_client import PluginRegistryClient
from fuckclassroom.web import create_app


class PluginRegistryInstalledUiTests(unittest.TestCase):
    def test_already_installed_release_has_a_green_check_immediately_on_render(self) -> None:
        row = {
            "id": "demo",
            "name": "Demo",
            "summary": "Demo plugin",
            "publisher": "example",
            "official": False,
            "status": "active",
            "installed_version": "0.1.0-beta.1",
            "target_version": "0.1.0-beta.1",
            "channel": "beta",
            "released_at": "2026-10-19",
            "action": "installed",
            "update_blocked": False,
            "compatibility_label": "✓ 兼容",
            "compatibility_message": "",
            "compatibility_severity": "success",
            "compatibility_state": "compatible",
            "signed": False,
            "missing_plugin_dependencies": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(AppConfig(data_dir=Path(directory)))
            with patch.object(PluginRegistryClient, "catalog", return_value=[row]):
                with TestClient(app) as client:
                    response = client.get("/plugins/registry/results")
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="plugin-market-installed"', response.text)
        self.assertIn('data-market-installed-status', response.text)
        self.assertIn("已安装</span>", response.text)
        self.assertNotIn("data-market-install-form", response.text)
        self.assertNotIn('data-market-installed-status aria-label="已安装"\n              hidden', response.text)

    def test_active_download_has_in_place_completion_target(self) -> None:
        template = Path(
            "fuckclassroom/plugins/core_ui/templates/core/plugin_registry_results.html"
        ).read_text(encoding="utf-8")
        script = Path(
            "fuckclassroom/plugins/core_ui/static/plugin_registry.js"
        ).read_text(encoding="utf-8")
        # The exact same element (not two lookalikes) is rendered for all
        # cards and unveiled after a successful installation.
        self.assertEqual(template.count('class="plugin-market-installed"'), 1)
        self.assertIn('data-market-installed-status', template)
        self.assertNotIn('data-market-done', template)
        self.assertNotIn('data-market-success-label', template)
        self.assertIn('ui.card.dataset.marketAction = "installed"', script)
        self.assertIn("ui.form.hidden = true", script)
        self.assertIn("ui.state.hidden = true", script)
        self.assertIn("ui.installedBadge.hidden = false", script)
        self.assertNotIn('ui.done.hidden = false', script)

    def test_install_button_uses_a_defined_theme_contrast_token(self) -> None:
        css = Path(
            "fuckclassroom/plugins/core_ui/static/core_ui.css"
        ).read_text(encoding="utf-8")
        tokens = Path("static/tokens.css").read_text(encoding="utf-8")
        self.assertIn("color: var(--primary-on)", css)
        self.assertNotIn("var(--on-primary)", css)
        self.assertIn("--primary-on: #ffffff", tokens)
        self.assertIn("--primary-on: #102039", tokens)
        self.assertIn(".plugin-market-installed[hidden]", css)
        self.assertIn(".plugin-market-installed svg circle", css)
        self.assertNotIn(".plugin-market-download-state.is-complete", css)


if __name__ == "__main__":
    unittest.main()
