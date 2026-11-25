"""Regression tests: downloading course PPT exports must not enter PJAX."""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CoursePptDownloadNavigationTests(unittest.TestCase):
    def test_course_ppt_link_bypasses_pjax(self):
        template = (ROOT / "fuckclassroom/plugins/classroom/templates/classroom/course_detail.html").read_text(encoding="utf-8")
        match = re.search(r'<a\b[^>]*\bdata-ppt-download\b[^>]*>', template)
        self.assertIsNotNone(match)
        self.assertIn("data-no-pjax", match.group())
        self.assertIn(" download", match.group())

    def test_shell_excludes_course_attachment_paths(self):
        shell = (ROOT / "static/shell.js").read_text(encoding="utf-8")
        self.assertIn("url.pathname.startsWith('/downloads/file/')", shell)
        self.assertIn(r"/download\/(?:ppt|transcript)", shell)
        self.assertIn("link.hasAttribute('data-no-pjax')", shell)
        self.assertIn("cleanupPageRuntime();", shell)

    def test_file_route_is_an_attachment(self):
        routes = (ROOT / "fuckclassroom/plugins/classroom/routes.py").read_text(encoding="utf-8")
        self.assertIn('"/courses/{course_id}/lessons/{lesson_id}/download/{export_kind}"', routes)
        self.assertIn("Content-Disposition", routes)


if __name__ == "__main__":
    unittest.main()
