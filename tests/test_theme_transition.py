from __future__ import annotations

import unittest
from pathlib import Path


class ThemeTransitionTests(unittest.TestCase):
    def test_theme_changes_use_a_reduced_motion_aware_transition(self) -> None:
        script = Path("static/app.js").read_text(encoding="utf-8")
        styles = Path("static/app.css").read_text(encoding="utf-8")

        self.assertIn('window.matchMedia("(prefers-reduced-motion: reduce)")', script)
        self.assertIn('root.classList.add("theme-transitioning")', script)
        self.assertIn('root.classList.remove("theme-transitioning")', script)
        self.assertIn("root.dataset.theme !== nextTheme", script)
        self.assertIn("@media (prefers-reduced-motion: no-preference)", styles)
        self.assertIn("html.theme-transitioning body *", styles)
        self.assertIn("transition-duration: 480ms", styles)
        self.assertIn("transition-timing-function: ease-in-out", styles)
        self.assertIn("}, 520);", script)


if __name__ == "__main__":
    unittest.main()
