from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

from fuckclassroom.config import AppConfig
from fuckclassroom.web import create_app


def _request(app) -> Request:
    return Request(
        {
            "type": "http",
            "app": app,
            "method": "GET",
            "path": "/settings",
            "raw_path": b"/settings",
            "query_string": b"",
            "headers": [],
            "client": ("test", 123),
            "server": ("test", 80),
            "scheme": "http",
            "root_path": "",
        }
    )


class SettingsPanelContextTests(unittest.TestCase):
    def test_core_only_settings_page_renders_without_feature_contexts(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            app = create_app(
                AppConfig(data_dir=Path(directory)),
                enabled_plugins=set(),
            )
            route = next(item for item in app.routes if getattr(item, "path", "") == "/settings")

            response = route.endpoint(_request(app))
            body = response.body.decode("utf-8")

            self.assertEqual(response.status_code, 200)
            self.assertIn('id="appearance"', body)
            self.assertIn('id="network"', body)
            self.assertIn('id="storage"', body)
            self.assertNotIn('id="courses"', body)
            self.assertNotIn('id="selection-network"', body)
            self.assertNotIn("Hy2", body)

    def test_classroom_settings_context_supplies_terms_to_its_panel(self) -> None:
        term = SimpleNamespace(id="2026-1", year="2026", season="1", current=True)
        snapshot = SimpleNamespace(terms=[term], error=None)

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, patch(
            "fuckclassroom.classroom.semester_sync.SemesterSyncService.sync",
            return_value=snapshot,
        ):
            app = create_app(
                AppConfig(data_dir=Path(directory)),
                enabled_plugins={"classroom"},
            )
            route = next(item for item in app.routes if getattr(item, "path", "") == "/settings")

            response = route.endpoint(_request(app))
            body = response.body.decode("utf-8")

            self.assertIn('id="courses"', body)
            self.assertIn('value="2026-1"', body)
            self.assertIn("2026 第 1 学期", body)


if __name__ == "__main__":
    unittest.main()
