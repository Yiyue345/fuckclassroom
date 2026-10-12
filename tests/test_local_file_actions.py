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
from tests.legacy_plugin_registry import install_legacy_external_plugins


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

    def test_library_validates_every_path_before_batch_deletion(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory) / "downloads"
            first = root / "course" / "slides.pdf"
            second = root / "course" / "notes.txt"
            first.parent.mkdir(parents=True)
            first.write_bytes(b"pdf")
            second.write_text("notes", encoding="utf-8")
            library = DownloadLibrary(root)

            with self.assertRaises(ValueError):
                library.delete_many(["course/slides.pdf", "../outside.txt"])
            self.assertTrue(first.exists())

            deleted = library.delete_many(
                ["course/slides.pdf", "course/notes.txt", "course/slides.pdf"]
            )

            self.assertEqual(deleted, 2)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertFalse((root / "course").exists())

    def test_local_file_route_dispatches_without_returning_file_content(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            data_dir = Path(directory)
            install_legacy_external_plugins(data_dir, {"classroom"})
            app = create_app(
                AppConfig(data_dir=data_dir),
                enabled_plugins={"classroom"},
            )
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
        template = Path("fuckclassroom/plugins/classroom/templates/classroom/downloads.html").read_text(encoding="utf-8")
        script = Path("fuckclassroom/plugins/classroom/static/downloads.js").read_text(encoding="utf-8")

        self.assertNotIn('href="/downloads/file/', template)
        self.assertIn('action="/downloads/local/open"', template)
        self.assertIn('action="/downloads/local/reveal"', template)
        self.assertIn('title="在文件夹中打开"', template)
        self.assertIn('form[data-local-file-action]', script)
        self.assertIn('headers: { Accept: "application/json" }', script)

    def test_batch_delete_route_accepts_multiple_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            data_dir = Path(directory)
            install_legacy_external_plugins(data_dir, {"classroom"})
            app = create_app(
                AppConfig(data_dir=data_dir),
                enabled_plugins={"classroom"},
            )
            route = next(
                item for item in app.routes if getattr(item, "path", "") == "/downloads/delete-many"
            )
            request = _post_request(
                app,
                "/downloads/delete-many",
                "relative_path=course%2Fslides.pdf&relative_path=course%2Fnotes.txt",
            )

            with patch.object(DownloadLibrary, "delete_many", return_value=2) as delete_many:
                response = asyncio.run(route.endpoint(request))
            payload = json.loads(response.body)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["deleted"], 2)
            delete_many.assert_called_once_with(["course/slides.pdf", "course/notes.txt"])

    def test_download_page_supports_visible_multi_selection(self) -> None:
        template = Path("fuckclassroom/plugins/classroom/templates/classroom/downloads.html").read_text(encoding="utf-8")
        script = Path("fuckclassroom/plugins/classroom/static/downloads.js").read_text(encoding="utf-8")

        self.assertIn("data-download-select-all", template)
        self.assertIn("data-download-delete-selected", template)
        self.assertIn('data-delete-endpoint="/downloads/delete-many"', template)
        self.assertIn("renderDownloadSelection", script)
        self.assertIn('requestBody.append("relative_path", checkbox.value)', script)


if __name__ == "__main__":
    unittest.main()
