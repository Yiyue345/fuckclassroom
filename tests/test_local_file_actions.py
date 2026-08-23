from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from fuckclassroom.classroom.downloads import DownloadLibrary
from fuckclassroom.config import AppConfig
from fuckclassroom.web import create_app


def _post_request(app, path: str, body: str) -> Request:
    payload = body.encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [(b"accept", b"application/json")],
            "client": ("test", 123),
            "server": ("test", 80),
            "scheme": "http",
            "root_path": "",
        },
        receive,
    )


class LocalFileActionTests(unittest.TestCase):
    def test_library_opens_and_reveals_only_resolved_files(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory) / "downloads"
            target = root / "course" / "slides.pdf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"pdf")
            library = DownloadLibrary(root)

            with patch("fuckclassroom.classroom.downloads._open_local_file") as open_file:
                self.assertEqual(library.open_file("course/slides.pdf"), target.resolve())
                open_file.assert_called_once_with(target.resolve())
            with patch("fuckclassroom.classroom.downloads._show_in_file_manager") as reveal_file:
                self.assertEqual(library.reveal_file("course/slides.pdf"), target.resolve())
                reveal_file.assert_called_once_with(target.resolve())
            with self.assertRaises(ValueError):
                library.open_file("../outside.txt")

    def test_local_file_route_dispatches_without_returning_file_content(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            app = create_app(AppConfig(data_dir=Path(directory)))
            route = next(
                item for item in app.routes if getattr(item, "path", "") == "/downloads/local/{action}"
            )

            for action, method_name in (("open", "open_file"), ("reveal", "reveal_file")):
                with self.subTest(action=action), patch.object(
                    DownloadLibrary,
                    method_name,
                    return_value=Path("slides.pdf"),
                ) as operation:
                    request = _post_request(
                        app,
                        f"/downloads/local/{action}",
                        "relative_path=course%2Fslides.pdf",
                    )
                    response = asyncio.run(route.endpoint(action, request))
                    payload = json.loads(response.body)

                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(payload["action"], action)
                    self.assertIn("slides.pdf", payload["message"])
                    operation.assert_called_once_with("course/slides.pdf")

    def test_download_page_uses_inline_local_file_actions(self) -> None:
        template = Path("templates/downloads.html").read_text(encoding="utf-8")
        script = Path("static/app.js").read_text(encoding="utf-8")

        self.assertNotIn('href="/downloads/file/', template)
        self.assertIn('action="/downloads/local/open"', template)
        self.assertIn('action="/downloads/local/reveal"', template)
        self.assertIn('title="在文件夹中打开"', template)
        self.assertIn('form[data-local-file-action]', script)
        self.assertIn('headers: { Accept: "application/json" }', script)


if __name__ == "__main__":
    unittest.main()
