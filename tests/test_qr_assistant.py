from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuckclassroom.classroom import LivePptSlide
from fuckclassroom.plugins.attendance.ketangpai import KetangpaiAuthenticationError
from fuckclassroom.plugins.attendance.scanner import (
    QRScannerUnavailable,
    WxScanService,
    analyze_qr_payload,
    parse_changke_scan_url,
    parse_changke_sign_payload,
)
from fuckclassroom.core.config import AppConfig
from fuckclassroom.plugins.attendance.qr import (
    register_qr_assistant_routes,
    submit_changke_qr,
    submit_ketangpai_qr,
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

    def test_frontend_wires_live_lessons_to_qr_endpoint(self) -> None:
        template = Path("fuckclassroom/plugins/classroom/templates/classroom/course_detail.html").read_text(encoding="utf-8")
        script = Path("fuckclassroom/plugins/attendance/static/qr_assistant.js").read_text(encoding="utf-8")

        self.assertIn('data-live-ppt-url="/api/courses/', template)
        self.assertIn("row.dataset.livePptUrl", script)
        self.assertIn('/${encodeURIComponent(slideId)}/qr', script)
        self.assertIn('"签到成功"', script)
        self.assertIn("await scanRequest(pending.pop())", script)

    def test_qr_endpoint_downloads_slide_and_returns_deduplicated_matches(self) -> None:
        classroom_client = Mock()
        scanner = Mock()
        slide = LivePptSlide(
            id=7,
            image_url="https://classroom.guet.edu.cn/ppt/7.png",
            thumbnail_url="",
            created_at="2026-09-18 10:00:00",
            offset_seconds=12,
        )
        classroom_client.list_live_ppt_slides.return_value = [slide]
        classroom_client.download_live_ppt_image.return_value = (b"png-bytes", "image/png")
        raw = "/scanner-jumper?" + urlencode({"p": "0~course-1!4~rollcall-9"})
        scanner.scan_image.return_value = [raw, raw, "hello world"]

        with tempfile.TemporaryDirectory() as directory:
            app = FastAPI()
            register_qr_assistant_routes(
                app,
                AppConfig(data_dir=Path(directory)),
                classroom_client=classroom_client,
                scanner=scanner,
            )
            response = TestClient(app).post(
                "/api/courses/course-1/lessons/lesson-2/live/ppt/7/qr"
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["matches"][0]["platform"], "changke")
        self.assertEqual(
            payload["matches"][0]["action_url"],
            f"https://courses.guet.edu.cn/{raw.lstrip('/')}",
        )
        classroom_client.download_live_ppt_image.assert_called_once_with(slide.image_url)
        scanner.scan_image.assert_called_once_with(b"png-bytes")

    def test_qr_endpoint_reports_missing_native_scanner(self) -> None:
        classroom_client = Mock()
        scanner = Mock()
        classroom_client.list_live_ppt_slides.return_value = [
            LivePptSlide(7, "https://classroom.guet.edu.cn/ppt/7.png", "", "", 0)
        ]
        classroom_client.download_live_ppt_image.return_value = (b"png-bytes", "image/png")
        scanner.scan_image.side_effect = QRScannerUnavailable("scanner unavailable")

        app = FastAPI()
        register_qr_assistant_routes(
            app,
            classroom_client=classroom_client,
            scanner=scanner,
        )
        response = TestClient(app).post(
            "/api/courses/course-1/lessons/lesson-2/live/ppt/7/qr"
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "scanner unavailable")

    def test_opencv_fallback_decodes_a_real_qr_image(self) -> None:
        import cv2

        image = cv2.QRCodeEncoder_create().encode("qr-fallback-ok")
        image = cv2.copyMakeBorder(image, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
        image = cv2.resize(image, None, fx=10, fy=10, interpolation=cv2.INTER_NEAREST)
        encoded, png = cv2.imencode(".png", image)
        self.assertTrue(encoded)

        with tempfile.TemporaryDirectory() as directory:
            service = WxScanService(AppConfig(data_dir=Path(directory)))
            with patch(
                "fuckclassroom.plugins.attendance.scanner.importlib.import_module",
                side_effect=ImportError("wxscan is not installed"),
            ):
                decoded = service.scan_image(png.tobytes())

        self.assertEqual(decoded, ["qr-fallback-ok"])

    def test_changke_qr_submission_maps_success_and_expiration(self) -> None:
        classroom_client = Mock()
        classroom_client.get_qr_device_id.return_value = "device-1"
        classroom_client.answer_qr_rollcall.return_value = {
            "_http_status": 200,
            "status": "on_call",
        }

        result = submit_changke_qr(
            classroom_client,
            {"rollcallId": 9, "data": "fresh-token"},
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["message"], "签到成功")
        classroom_client.answer_qr_rollcall.assert_called_once_with(
            "9", "fresh-token", "device-1"
        )

        classroom_client.answer_qr_rollcall.return_value = {
            "_http_status": 400,
            "message": "QR_code_expired",
        }
        expired = submit_changke_qr(
            classroom_client,
            {"rollcallId": 9, "data": "old-token"},
        )
        self.assertEqual(expired["state"], "expired")
        self.assertFalse(expired["retryable"])
        self.assertIn("二维码已过期", expired["message"])

    def test_ketangpai_qr_submission_reports_login_and_success(self) -> None:
        ketangpai_client = Mock()
        ketangpai_client.submit_scan.side_effect = KetangpaiAuthenticationError(
            "请先登录课堂派"
        )

        missing = submit_ketangpai_qr(
            ketangpai_client,
            "https://www.ketangpai.com/sign/scan?ticketid=t1&expire=e1&sign=s1",
        )

        self.assertFalse(missing["attempted"])
        self.assertEqual(missing["state"], "login_required")
        self.assertEqual(missing["account_url"], "/accounts#ketangpai-session")

        ketangpai_client.submit_scan.side_effect = None
        ketangpai_client.submit_scan.return_value = {
            "status": 1,
            "data": {"state": 8, "info": "签到成功"},
        }
        result = submit_ketangpai_qr(
            ketangpai_client,
            "https://www.ketangpai.com/sign/scan?ticketid=t1&expire=e1&sign=s1",
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["message"], "签到成功")

    def test_http_endpoint_decodes_and_classifies_a_real_qr_image(self) -> None:
        import cv2

        raw = "/scanner-jumper?" + urlencode(
            {"p": "0~course-1!3~fresh-token!4~9"}
        )
        image = cv2.QRCodeEncoder_create().encode(raw)
        image = cv2.copyMakeBorder(image, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
        image = cv2.resize(image, None, fx=10, fy=10, interpolation=cv2.INTER_NEAREST)
        encoded, png = cv2.imencode(".png", image)
        self.assertTrue(encoded)

        classroom_client = Mock()
        classroom_client.list_live_ppt_slides.return_value = [
            LivePptSlide(7, "https://classroom.guet.edu.cn/ppt/7.png", "", "", 0)
        ]
        classroom_client.download_live_ppt_image.return_value = (png.tobytes(), "image/png")
        classroom_client.get_qr_device_id.return_value = "device-1"
        classroom_client.answer_qr_rollcall.return_value = {
            "_http_status": 200,
            "status": "on_call",
        }

        with tempfile.TemporaryDirectory() as directory:
            app = FastAPI()
            register_qr_assistant_routes(
                app,
                AppConfig(data_dir=Path(directory)),
                classroom_client=classroom_client,
                scanner=WxScanService(AppConfig(data_dir=Path(directory))),
            )
            with patch(
                "fuckclassroom.plugins.attendance.scanner.importlib.import_module",
                side_effect=ImportError("wxscan is not installed"),
            ):
                response = TestClient(app).post(
                    "/api/courses/course-1/lessons/lesson-2/live/ppt/7/qr"
                )

        self.assertEqual(response.status_code, 200)
        match = response.json()["matches"][0]
        self.assertEqual(match["platform"], "changke")
        self.assertEqual(match["kind"], "rollcall")
        self.assertEqual(match["fields"]["rollcallId"], "9")
        self.assertTrue(match["sign"]["success"])
        self.assertEqual(match["sign"]["message"], "签到成功")


if __name__ == "__main__":
    unittest.main()
