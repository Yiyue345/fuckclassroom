from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fuckclassroom.core.config import AppConfig
from .errors import AIClientError


@dataclass(frozen=True)
class AIClient:
    config: AppConfig

    def summarize_course_text(self, text: str) -> str:
        if not text.strip():
            raise AIClientError("没有可总结的文字内容")

        chunks = _chunk_text(text, 12000)
        partials = [
            self._chat(
                [
                    {
                        "role": "system",
                        "content": "你是课程助教，负责从课堂转写和课件 OCR 中提炼结构化中文学习笔记。",
                    },
                    {
                        "role": "user",
                        "content": (
                            "请整理下面这段课程材料，保留重要概念、例子、作业/考试提醒。"
                            "输出要点式中文笔记：\n\n"
                            + chunk
                        ),
                    },
                ],
                model=self.config.ai_model,
            )
            for chunk in chunks
        ]

        if len(partials) == 1:
            return partials[0]

        return self._chat(
            [
                {
                    "role": "system",
                    "content": "你是课程助教，负责合并多段课堂笔记，生成不重复、结构清晰的中文总结。",
                },
                {
                    "role": "user",
                    "content": (
                        "请把下面多段阶段性笔记合并成一份课程总结，包含：核心知识点、课堂例子、"
                        "可能的作业/考试提醒、课后复习建议。\n\n"
                        + "\n\n---\n\n".join(partials)
                    ),
                },
            ],
            model=self.config.ai_model,
        )

    def ocr_image(self, image_bytes: bytes, media_type: str) -> str:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return self._chat(
            [
                {
                    "role": "system",
                    "content": "你是 OCR 引擎。只提取图片中的可见文字，保持原有语义顺序，不要总结。",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请提取这页课件图片中的所有中文和英文文字。"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                        },
                    ],
                },
            ],
            model=self.config.ocr_model,
        )

    def _chat(self, messages: list[dict[str, Any]], model: str) -> str:
        if not self.config.ai_api_key:
            raise AIClientError("缺少 FUCKCLASSROOM_AI_API_KEY，无法调用 AI")

        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.config.ai_base_url}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.ai_api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=120) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AIClientError(f"AI 请求失败：{exc.code} {detail[:300]}") from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIClientError("AI 响应格式不符合预期") from exc
        return str(content).strip()


def _chunk_text(text: str, max_chars: int) -> list[str]:
    lines = text.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks or [text[:max_chars]]
