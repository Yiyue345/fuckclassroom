from __future__ import annotations

import re
import unittest
from pathlib import Path


class ThemeTransitionTests(unittest.TestCase):
    def test_theme_changes_use_one_visible_color_transition_duration(self) -> None:
        script = Path("static/app.js").read_text(encoding="utf-8")
        styles = Path("static/app.css").read_text(encoding="utf-8")
        template = Path("templates/base.html").read_text(encoding="utf-8")

        self.assertNotIn('window.matchMedia("(prefers-reduced-motion: reduce)")', script)
        self.assertIn('root.classList.add("theme-transitioning")', script)
        self.assertIn('root.classList.remove("theme-transitioning")', script)
        self.assertIn("root.dataset.theme !== nextTheme", script)
        self.assertIn("html.theme-transitioning body *", styles)
        self.assertIsNotNone(re.search(r"--theme-transition-duration:\s+\d+ms", styles))
        self.assertIn('@property --text { syntax: "<color>"; inherits: true;', styles)
        self.assertIn("transition-property: --bg, --surface", styles)
        self.assertNotIn("transition-property: background-color, border-color, color", styles)
        self.assertIn("transition-duration: var(--theme-transition-duration)", styles)
        self.assertIn("transition-timing-function: ease-in-out", styles)
        self.assertIn("themeTransitionDurationMs() + 40", script)
        self.assertIn('href="/static/app.css?v=', template)
        self.assertIn('src="/static/app.js?v=', template)


if __name__ == "__main__":
    unittest.main()
