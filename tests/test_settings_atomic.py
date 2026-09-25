from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from fuckclassroom.auth.credentials import CredentialStoreError
from fuckclassroom.core.config import AppConfig
from fuckclassroom.web import create_app


class SettingsAtomicPersistenceTests(unittest.TestCase):
    def test_credential_failure_rolls_back_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(data_dir=Path(directory))
            app = create_app(config, enabled_plugins=("core_ui",))
            services = app.state.plugin_runtime.services
            settings_store = services.get("settings_store")
            credential_store = services.get("credential_store")
            settings_store.update_from_form({"ai_model": "before-model"})
            before = settings_store.path.read_bytes()

            with (
                patch.object(
                    credential_store,
                    "update_from_form",
                    side_effect=CredentialStoreError("credential write failed"),
                ),
                TestClient(app) as client,
            ):
                response = client.post(
                    "/settings",
                    data={
                        "ai_model": "after-model",
                        "account_username": "student",
                        "account_password": "password",
                    },
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 303)
            self.assertIn("credential_error=", response.headers["location"])
            self.assertEqual(settings_store.path.read_bytes(), before)
            self.assertEqual(settings_store.load().ai_model, "before-model")


if __name__ == "__main__":
    unittest.main()
