from __future__ import annotations

import json
import unittest
from urllib.parse import urlencode

from fuckclassroom.classroom.qr_scan import (
    analyze_qr_payload,
    parse_changke_scan_url,
    parse_changke_sign_payload,
)


class QrAssistantTests(unittest.TestCase):
    def test_changke_compact_payload_decodes_named_fields_and_types(self) -> None:
        payload = "!".join(
            [
                "0~course-1",
                "1~activity-2",
                f"2~{chr(26)}2",
                "4~rollcall-9",
                f"8~{chr(26)}1",
                f"6~A{chr(31)}B{chr(30)}C",
            ]
        )

        decoded = parse_changke_sign_payload(payload)

        self.assertEqual(decoded["courseId"], "course-1")
        self.assertEqual(decoded["activityId"], "activity-2")
        self.assertEqual(decoded["activityType"], "classroom-exam")
        self.assertEqual(decoded["rollcallId"], "rollcall-9")
        self.assertTrue(decoded["enableGroupRollcall"])
        self.assertEqual(decoded["accessCode"], "A~B!C")

    def test_changke_scanner_jumper_url_is_classified(self) -> None:
        compact = "0~course-1!4~rollcall-9"
        raw = "/scanner-jumper?" + urlencode({"p": compact})

        fields = parse_changke_scan_url(raw)
        match = analyze_qr_payload(raw)

        self.assertEqual(fields, {"courseId": "course-1", "rollcallId": "rollcall-9"})
        self.assertEqual(match.platform, "changke")
        self.assertEqual(match.kind, "rollcall")
        self.assertEqual(match.action_url, "")

    def test_changke_json_payload_keeps_official_url_for_user_action(self) -> None:
        raw = "https://example.edu/j?" + urlencode(
            {"_p": json.dumps({"courseId": "c1", "rollcallId": "r1"})}
        )

        match = analyze_qr_payload(raw)

        self.assertEqual(match.platform, "changke")
        self.assertEqual(match.fields["rollcallId"], "r1")
        self.assertEqual(match.action_url, raw)

    def test_ketangpai_link_is_detected_without_submitting_it(self) -> None:
        raw = "https://www.ketangpai.com/sign/scan?code=abc"

        match = analyze_qr_payload(raw)

        self.assertEqual(match.platform, "ketangpai")
        self.assertEqual(match.kind, "rollcall-link")
        self.assertEqual(match.action_url, raw)

    def test_generic_qr_stays_generic(self) -> None:
        match = analyze_qr_payload("hello world")
        self.assertEqual(match.platform, "unknown")
        self.assertEqual(match.raw, "hello world")


if __name__ == "__main__":
    unittest.main()
