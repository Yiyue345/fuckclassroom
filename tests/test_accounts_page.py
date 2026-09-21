from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from fuckclassroom.config import AppConfig
from fuckclassroom.web import create_app


class AccountsPageTests(unittest.TestCase):
    def test_accounts_page_contains_both_session_panels_and_sidebar_states(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            app = create_app(AppConfig(data_dir=Path(directory)))
            route = next(item for item in app.routes if getattr(item, "path", "") == "/accounts")
            request = Request(
                {
                    "type": "http",
                    "app": app,
                    "method": "GET",
                    "path": "/accounts",
                    "raw_path": b"/accounts",
                    "query_string": b"",
                    "headers": [],
                    "client": ("test", 123),
                    "server": ("test", 80),
                    "scheme": "http",
                    "root_path": "",
                }
            )

            response = route.endpoint(request)
            body = response.body.decode("utf-8")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.template.name, "core/accounts.html")
            self.assertIn('id="classroom-session"', body)
            self.assertIn('id="selection-session"', body)
            self.assertIn('id="ketangpai-session"', body)
            self.assertIn('data-sidebar-session="classroom"', body)
            self.assertIn('data-sidebar-session="selection"', body)
            self.assertIn('action="/auth/start"', body)
            self.assertIn('action="/selection/login"', body)
            self.assertIn('action="/accounts/ketangpai/login"', body)
            self.assertIn('action="/accounts/ketangpai/sms/start"', body)

    def test_legacy_selection_session_page_redirects_to_accounts(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            app = create_app(AppConfig(data_dir=Path(directory)))
            route = next(item for item in app.routes if getattr(item, "path", "") == "/selection")

            response = route.endpoint()

            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers["location"], "/accounts#selection-session")


if __name__ == "__main__":
    unittest.main()
