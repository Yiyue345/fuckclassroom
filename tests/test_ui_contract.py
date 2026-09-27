from pathlib import Path
from types import SimpleNamespace as NS
import unittest

from jinja2 import Environment, FileSystemLoader, select_autoescape

from fuckclassroom.core.plugins import PluginRegistry, PluginSpec, UIAsset, UISlot
from fuckclassroom.core.settings import AppSettings
from fuckclassroom.plugins import build_plugin_registry


ALL = {"classroom", "course_selection", "processing", "transcription", "ocr", "ai_summary", "attendance"}


def environment(registry):
    return Environment(
        loader=FileSystemLoader(registry.template_directories(Path("templates"))),
        autoescape=select_autoescape(["html"]),
    )


def request(registry):
    return NS(app=NS(state=NS(
        plugin_registry=registry,
        get_classroom_session_status=lambda: {},
        get_selection_session_status=lambda: {},
    )))


class UIContractTests(unittest.TestCase):
    def test_legacy_and_page_assets_are_ordered_and_deduplicated(self):
        registry = PluginRegistry([
            PluginSpec(id="a", name="A", scripts=("/static/shared.js",), ui_assets=(
                UIAsset("/static/shared.js"), UIAsset("page.js", pages=("detail",)),
            )),
            PluginSpec(id="b", name="B", ui_assets=(UIAsset("extension.js", pages=("detail",)),)),
        ])
        self.assertEqual(registry.page_assets("other", "script"), ("/static/shared.js",))
        self.assertEqual(registry.page_assets("detail", "script"), (
            "/static/shared.js", "/plugins/a/static/page.js", "/plugins/b/static/extension.js",
        ))

    def test_assets_are_local_and_kinds_are_validated(self):
        for path in ("", "//example.com/a.js", "https://example.com/a.js", "../other.js", "folder/../other.js"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                UIAsset(path)
        with self.assertRaises(ValueError):
            UIAsset("page.js", "unknown")

    def test_slot_order_and_disabled_contributors(self):
        a = PluginSpec(id="a", name="A", ui_slots=(UISlot("actions", "a.html", 20),))
        b = PluginSpec(id="b", name="B", ui_slots=(UISlot("actions", "b.html", 10),))
        self.assertEqual(PluginRegistry([a, b]).slot_templates("actions"), ("b.html", "a.html"))
        self.assertEqual(PluginRegistry([a]).slot_templates("actions"), ("a.html",))
        self.assertEqual(PluginRegistry([a]).slot_templates("unknown"), ())

    def test_all_enabled_templates_compile(self):
        env = environment(build_plugin_registry(ALL))
        for name in env.list_templates():
            with self.subTest(template=name):
                env.get_template(name)

    def test_shared_fields_escape_values_and_preserve_form_semantics(self):
        env = environment(build_plugin_registry(set()))
        template = env.from_string('{% import "ui/components.html" as ui %}{{ ui.field("count", "Count", 0, type="number", min=0, max=8, hint="Help") }}{{ ui.field("text", "Text", payload) }}{{ ui.checkbox("enabled", "Enable", true) }}{{ ui.checkbox("disabled", "Disable", false) }}')
        html = template.render(payload='"><script>alert(1)</script>')
        self.assertIn('value="0"', html)
        self.assertIn('min="0" max="8"', html)
        self.assertIn('aria-describedby="count-hint"', html)
        self.assertIn('name="enabled" checked', html)
        self.assertNotIn('name="disabled" checked', html)
        self.assertNotIn('<script>', html)

    def test_settings_panels_preserve_saved_selects_and_numeric_limits(self):
        env = environment(build_plugin_registry(ALL))
        settings = AppSettings().to_public_dict()
        settings.update(
            asr_device="cuda",
            asr_compute_type="float16",
            asr_cpu_threads=4,
            asr_use_batched=True,
        )
        runtime = {
            "cuda_available": True,
            "cuda_reason": "",
            "cuda_device_count": 1,
            "auto_device": "cuda",
            "ctranslate2_version": "4.8.2",
            "compute_types": {
                "cpu": ["int8", "float32", "default"],
                "cuda": ["int8", "int8_float16", "float16", "float32", "default"],
            },
        }
        html = env.get_template("transcription_settings.html").render(
            settings=settings,
            panel_context={"asr_runtime": runtime},
        )
        self.assertIn('value="cuda" selected', html)
        self.assertIn('value="float16" selected', html)
        self.assertIn('name="asr_cpu_threads" type="number" value="4" min="1" max="64" step="1"', html)

    def test_transcription_settings_disable_unavailable_cuda_and_compute_types(self):
        env = environment(build_plugin_registry(ALL))
        settings = AppSettings().to_public_dict()
        settings.update(asr_device="cuda", asr_compute_type="float16")
        runtime = {
            "cuda_available": False,
            "cuda_reason": "缺少或无法加载 CUDA 12 cuBLAS",
            "cuda_device_count": 1,
            "auto_device": "cpu",
            "ctranslate2_version": "4.8.2",
            "compute_types": {
                "cpu": ["int8", "float32", "default"],
                "cuda": [],
            },
        }

        html = env.get_template("transcription_settings.html").render(
            settings=settings,
            panel_context={"asr_runtime": runtime},
        )

        self.assertIn('value="cuda" disabled', html)
        self.assertIn("cuda（环境不可用）", html)
        self.assertIn("缺少或无法加载 CUDA 12 cuBLAS", html)
        self.assertIn('value="float16" disabled', html)
        self.assertIn('value="cpu" selected', html)
        self.assertIn('name="asr_use_batched" checked', html)

    def test_course_page_loses_optional_controls_and_assets_when_disabled(self):
        lesson = NS(id="1", title="Lesson", resources=[NS(kind="video", is_downloadable=True, url="/demo.mp4")], is_live=False)
        detail = NS(course=NS(id="1", title="Course"), lessons=[lesson])
        for enabled, has_processing, has_attendance in [({"classroom"}, False, False), ({"processing"}, True, False), (ALL, True, True)]:
            registry = build_plugin_registry(enabled)
            html = environment(registry).get_template("classroom/course_detail.html").render(request=request(registry), detail=detail)
            with self.subTest(enabled=enabled):
                self.assertEqual('/lessons/1/extract' in html, has_processing)
                self.assertEqual('task_detail_progress.js' in html, has_processing)
                self.assertEqual('qr_assistant.js' in html, has_attendance)
                self.assertNotIn('selection_ui.js', html)
                self.assertLess(html.index('hls.min.js'), html.index('course_media.js'))

    def test_ai_action_is_contributed_only_while_enabled(self):
        for enabled, expected in [({"processing"}, False), ({"ai_summary"}, True)]:
            registry = build_plugin_registry(enabled)
            html = environment(registry).get_template("processing/lesson_outputs.html").render(request=request(registry), outputs={}, course_id="1", lesson_id="2")
            self.assertEqual('/lessons/2/summarize' in html, expected)


if __name__ == "__main__":
    unittest.main()
