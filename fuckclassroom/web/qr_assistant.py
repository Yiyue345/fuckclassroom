from __future__ import annotations

from urllib.parse import urljoin

from fastapi import FastAPI, HTTPException

from ..classroom import ClassroomClient, ClassroomClientError
from ..classroom.qr_scan import (
    QRScannerError,
    QRScannerUnavailable,
    WxScanService,
    analyze_qr_payload,
)
from ..core.config import AppConfig


GUET_CHANGKE_BASE_URL = "https://courses.guet.edu.cn/"


def register_qr_assistant_routes(app: FastAPI, config: AppConfig | None = None) -> None:
    app_config = config or AppConfig()
    classroom_client = ClassroomClient(app_config)
    scanner = WxScanService(app_config)

    @app.post("/api/courses/{course_id}/lessons/{lesson_id}/live/ppt/{slide_id}/qr")
    def scan_live_ppt_qr(course_id: str, lesson_id: str, slide_id: int) -> dict[str, object]:
        if slide_id <= 0:
            raise HTTPException(status_code=400, detail="直播 PPT 页编号无效")
        try:
            slide = next(
                (
                    item
                    for item in classroom_client.list_live_ppt_slides(course_id, lesson_id)
                    if item.id == slide_id
                ),
                None,
            )
            if slide is None:
                raise HTTPException(status_code=404, detail="未找到这页直播 PPT")
            image_bytes, _ = classroom_client.download_live_ppt_image(slide.image_url)
            decoded = scanner.scan_image(image_bytes)
        except HTTPException:
            raise
        except ClassroomClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except QRScannerUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except QRScannerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        unique_payloads = list(dict.fromkeys(decoded))
        matches: list[dict[str, object]] = []
        for raw in unique_payloads:
            match = analyze_qr_payload(raw).to_dict()
            if match["platform"] == "changke" and not match["action_url"] and raw.startswith("/"):
                match["action_url"] = urljoin(GUET_CHANGKE_BASE_URL, raw)
            matches.append(match)
        return {
            "slide_id": slide.id,
            "created_at": slide.created_at,
            "matches": matches,
            "count": len(matches),
        }
