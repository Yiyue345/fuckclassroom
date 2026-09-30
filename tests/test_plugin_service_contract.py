from __future__ import annotations

import unittest

from fuckclassroom.core.plugins import PluginServiceError
from fuckclassroom.plugins.ai_summary.errors import AIClientError
from fuckclassroom.plugins.processing.extraction import LessonExtractionService
from fuckclassroom.plugins.transcription.errors import LocalTranscriptionError


class PluginServiceContractTests(unittest.TestCase):
    def test_plugin_specific_errors_share_stable_cross_plugin_base(self) -> None:
        self.assertTrue(issubclass(LocalTranscriptionError, PluginServiceError))
        self.assertTrue(issubclass(AIClientError, PluginServiceError))

    def test_processing_can_construct_without_importing_sibling_plugins(self) -> None:
        service = LessonExtractionService.__new__(LessonExtractionService)
        # Constructor behavior is exercised with a patched classroom below so this
        # test does not need network access or optional plugin dependencies.
        from fuckclassroom.plugins.processing import extraction

        class FakeClassroom:
            pass

        original = extraction.ClassroomClient
        extraction.ClassroomClient = lambda config: FakeClassroom()  # type: ignore[assignment]
        try:
            service.__init__(service_lookup=None)
        finally:
            extraction.ClassroomClient = original  # type: ignore[assignment]

        self.assertIsNone(service.transcription)
        self.assertIsNone(service.ai)
        self.assertIsNone(service._legacy_ocr)


if __name__ == "__main__":
    unittest.main()
