from __future__ import annotations

import json
import tempfile
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import Mock, patch

from fuckclassroom.classroom.client import (
    ClassroomClient,
    ClassroomClientError,
    Course,
    CourseDetail,
    is_allowed_live_media_url,
    rewrite_live_playlist,
)
from fuckclassroom.core.config import AppConfig
from fuckclassroom.plugins.classroom.routes import _reconcile_live_status


class LiveClassroomTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.client = ClassroomClient(AppConfig(data_dir=Path(self.temporary.name)))

    def test_live_list_is_filtered_to_enrolled_courses(self) -> None:
        self.client._get_json = lambda _path, _params=None: {  # type: ignore[method-assign]
            "code": 0,
            "list": [
                {
                    "id": "course-1",
                    "title": "数据结构",
                    "sub_id": "lesson-8",
                    "sub_title": "二叉树",
                    "sub_status": "1",
                    "lecturer_name": "张老师",
                    "room_name": "A101",
                },
                {"id": "other-course", "sub_id": "x", "sub_status": "1"},
            ],
        }
        courses = [
            Course("course-1", "数据结构", "张老师", "", "", "", "", raw={"TenantCode": 21})
        ]

        live = self.client.list_current_live_lessons(courses)

        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].lesson_id, "lesson-8")
        self.assertEqual(live[0].room_name, "A101")

    def test_course_lesson_parser_marks_running_lesson_live(self) -> None:
        lesson = self.client._lesson_from_row(
            {
                "id": "lesson-8",
                "title": "二叉树",
                "status_label": "未开始",
                "content": {"api_pass": {"ppt_status": "running"}},
            },
            "course-1",
        )

        self.assertTrue(lesson.is_live)
        self.assertEqual(lesson.status_label, "直播中")

    def test_live_list_clears_stale_running_flag_after_recording_exists(self) -> None:
        lesson = self.client._lesson_from_row(
            {
                "id": "lesson-8",
                "title": "二叉树",
                "content": {
                    "api_pass": {"ppt": "token", "ppt_status": "running"},
                    "file_list": [
                        {
                            "file_name": "https://classroom.guet.edu.cn/play/recording.mp4",
                            "file_type": "mp4",
                        }
                    ],
                },
            },
            "course-1",
        )
        course = Course("course-1", "数据结构", "张老师", "", "", "", "")
        reconciled = _reconcile_live_status(
            CourseDetail(course=course, lessons=[lesson]),
            set(),
        )

        self.assertFalse(reconciled.lessons[0].is_live)
        self.assertEqual(reconciled.lessons[0].status_label, "录播已生成")

    def test_live_info_exposes_available_qualities(self) -> None:
        self.client._get_json = lambda _path, _params=None: {  # type: ignore[method-assign]
            "code": 0,
            "data": {
                "course_id": "course-1",
                "sub_id": "lesson-8",
                "sub_status": "1",
                "tenant_code": "21",
                "content": {"api_pass": {"ppt_status": "running"}},
                "live_url": {
                    "output": {
                        "m3u8": "https://classroom.guet.edu.cn/pgc/live/index.m3u8?auth_key=x",
                        "m3u8_lhd": "https://classroom.guet.edu.cn/pgc/live/high.m3u8?auth_key=x",
                    }
                },
            },
        }

        info = self.client.get_live_lesson_info("course-1", "lesson-8")

        self.assertTrue(info.is_live)
        self.assertTrue(info.ppt_running)
        self.assertEqual(set(info.playlist_urls), {"original", "high"})
        self.assertIn("course_id=course-1", info.official_url)

    def test_live_ppt_rows_are_parsed_and_incremental(self) -> None:
        self.client._get_json = lambda _path, _params=None: {  # type: ignore[method-assign]
            "code": 0,
            "list": [
                {
                    "id": 11,
                    "created_sec": 23,
                    "content": json.dumps(
                        {
                            "pptimgurl": "https://classroom.guet.edu.cn/play/ppt/11.png",
                            "pptthumb": "https://classroom.guet.edu.cn/play/ppt/thumb-11.png",
                        }
                    ),
                },
                {
                    "id": 10,
                    "content": json.dumps(
                        {"pptimgurl": "https://classroom.guet.edu.cn/play/ppt/10.png"}
                    ),
                },
            ],
        }

        slides = self.client.list_live_ppt_slides("course-1", "lesson-8", after_id=10)

        self.assertEqual([slide.id for slide in slides], [11])
        self.assertEqual(slides[0].offset_seconds, 23)

    def test_live_ppt_rows_follow_platform_pagination(self) -> None:
        requested_pages: list[int] = []

        def get_json(_path, params=None):
            page = int(params["page"])
            requested_pages.append(page)
            start = (page - 1) * 100 + 1
            remaining = max(0, 131 - start + 1)
            rows = [
                {
                    "id": index,
                    "content": json.dumps(
                        {"pptimgurl": f"https://classroom.guet.edu.cn/play/ppt/{index}.jpg"}
                    ),
                }
                for index in range(start, start + min(100, remaining))
            ]
            return {"code": 0, "total": 131, "list": rows}

        self.client._get_json = get_json  # type: ignore[method-assign]

        slides = self.client.list_live_ppt_slides("course-1", "lesson-8")

        self.assertEqual(len(slides), 131)
        self.assertEqual(requested_pages, [1, 2])
        self.assertEqual(slides[-1].id, 131)

    def test_qr_device_id_is_stable_and_locally_persisted(self) -> None:
        first = self.client.get_qr_device_id()
        second = ClassroomClient(self.client.config).get_qr_device_id()

        self.assertEqual(first, second)
        self.assertTrue((self.client.config.data_dir / "account" / "qr_device.json").is_file())

    def test_qr_rollcall_uses_official_put_endpoint_and_session(self) -> None:
        response = Mock()
        response.status = 200
        response.read.return_value = b'{"id":7,"status":"on_call"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response

        with (
            patch.object(self.client, "_ensure_changke_session", return_value="session-1"),
            patch.object(self.client, "_cookie_jar", return_value=CookieJar()),
            patch("fuckclassroom.classroom.client.build_opener", return_value=opener),
        ):
            payload = self.client.answer_qr_rollcall("9", "fresh-token", "device-1")

        self.assertEqual(payload["status"], "on_call")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "PUT")
        self.assertEqual(
            request.full_url,
            "https://courses.guet.edu.cn/api/rollcall/9/answer_qr_rollcall",
        )
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"data": "fresh-token", "deviceId": "device-1"},
        )
        self.assertEqual(request.headers["X-session-id"], "session-1")

    def test_hls_rewrite_only_allows_platform_media(self) -> None:
        source = "https://classroom.guet.edu.cn/pgc/live/master.m3u8?auth_key=x"
        output = rewrite_live_playlist(
            '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\nsegment.ts?auth_key=x\n',
            source,
        )

        self.assertIn("%2Fpgc%2Flive%2Fkey.bin", output)
        self.assertIn("%2Fpgc%2Flive%2Fsegment.ts%3Fauth_key%3Dx", output)
        self.assertTrue(is_allowed_live_media_url(source))
        self.assertFalse(is_allowed_live_media_url("https://evil.example/pgc/live/master.m3u8"))
        with self.assertRaises(ClassroomClientError):
            rewrite_live_playlist("https://evil.example/segment.ts\n", source)

    def test_templates_include_live_entry_and_player_hooks(self) -> None:
        overview = Path("fuckclassroom/plugins/classroom/templates/classroom/login.html").read_text(encoding="utf-8")
        detail = Path("fuckclassroom/plugins/classroom/templates/classroom/course_detail.html").read_text(encoding="utf-8")
        base = Path("templates/base.html").read_text(encoding="utf-8")
        styles = Path("fuckclassroom/plugins/classroom/static/course_media.css").read_text(encoding="utf-8")
        script = Path("fuckclassroom/plugins/classroom/static/course_media.js").read_text(encoding="utf-8")

        self.assertIn("正在直播", overview)
        self.assertIn("live-now-icon", overview)
        self.assertIn("app.css?v=20260921-3", base)
        self.assertIn("data-live-info-url", detail)
        self.assertIn("data-ppt-slides-url", detail)
        self.assertIn("进入直播", detail)
        self.assertIn("window.Hls", script)
        self.assertIn("window.setTimeout(poll, 3000)", script)
        self.assertIn('pptStage.style.aspectRatio', script)
        self.assertIn("snapshotSlides.length > exportSlides.length", script)
        self.assertIn("aspect-ratio: 16 / 9", styles)


if __name__ == "__main__":
    unittest.main()
