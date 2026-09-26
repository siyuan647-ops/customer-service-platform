from __future__ import annotations

import asyncio
import base64
import json

from openai import AsyncOpenAI

from backend.app.agents.contracts import EvidencePrecheckResult
from backend.app.config import Settings


def normalize_evidence_payload(payload: dict) -> dict:
    """Normalize small, known OpenAI-compatible provider schema deviations."""
    normalized_payload = dict(payload)
    confidence = normalized_payload.get("confidence")
    if isinstance(confidence, str):
        normalized = confidence.strip().lower()
        labels = {"high": 0.9, "medium": 0.6, "low": 0.3}
        if normalized in labels:
            normalized_payload["confidence"] = labels[normalized]
        elif normalized.endswith("%"):
            normalized_payload["confidence"] = float(normalized[:-1]) / 100
    return normalized_payload


class EvidenceAnalyzer:
    """Produces advisory-only structured facts from customer evidence media."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def analyze(
        self,
        *,
        data: bytes,
        content_type: str,
        filename: str,
    ) -> EvidencePrecheckResult:
        if self.settings.agent_mode == "mock":
            return EvidencePrecheckResult(
                media_quality="clear",
                issue_visible=True,
                detected_issues=["测试环境模拟问题可见"],
                product_visible=True,
                package_visible=False,
                waybill_visible=False,
                extracted_tracking_number=None,
                summary="测试环境多模态预审完成，具体材料仍需人工审核。",
                risk_flags=[],
                confidence=0.8,
            )
        return await self._analyze_live(
            data=data,
            content_type=content_type,
            filename=filename,
        )

    async def _analyze_live(
        self,
        *,
        data: bytes,
        content_type: str,
        filename: str,
    ) -> EvidencePrecheckResult:
        if not self.settings.kimi_api_key:
            raise RuntimeError("KIMI_API_KEY is required for evidence analysis")
        encoded = base64.b64encode(data).decode("ascii")
        media_type = "video_url" if content_type.startswith("video/") else "image_url"
        media_part = {
            "type": media_type,
            media_type: {"url": f"data:{content_type};base64,{encoded}"},
        }
        client = AsyncOpenAI(
            api_key=self.settings.kimi_api_key,
            base_url=self.settings.kimi_base_url,
            timeout=self.settings.evidence_analysis_timeout_seconds,
            max_retries=0,
        )
        try:
            async with asyncio.timeout(
                self.settings.evidence_analysis_timeout_seconds
            ):
                response = await client.chat.completions.create(
                    model=self.settings.kimi_model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是电商售后材料预审助手。只描述媒体中直接可见的事实，"
                                "不要判断退款资格、责任归属、欺诈或最终审核结果。不得识别或"
                                "输出姓名、手机号、地址等个人信息。仅在快递面单清晰可见时"
                                "提取运单号。必须输出JSON对象，不要输出Markdown。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": [
                                media_part,
                                {
                                    "type": "text",
                                    "text": (
                                        "预审这份售后材料。返回字段：media_quality只能为"
                                        "clear、blurry、unusable；issue_visible；detected_issues；"
                                        "product_visible；package_visible；waybill_visible；"
                                        "extracted_tracking_number；summary；risk_flags；confidence。"
                                        "所有可见性字段必须是JSON布尔值；detected_issues和"
                                        "risk_flags必须是字符串数组；confidence必须是0到1之间"
                                        "的JSON数字，不能返回high、medium或low。"
                                        "risk_flags只记录材料不清晰、内容不一致或疑似重复等需要"
                                        "人工关注的现象，不得直接认定造假。"
                                    ),
                                },
                            ],
                        },
                    ],
                    response_format={"type": "json_object"},
                    extra_body={"thinking": {"type": "disabled"}},
                    max_tokens=1200,
                )
            content = response.choices[0].message.content
            if not content:
                raise RuntimeError("Evidence analyzer returned an empty response")
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError("Evidence analyzer response must be a JSON object")
            return EvidencePrecheckResult.model_validate(
                normalize_evidence_payload(payload)
            )
        finally:
            await client.close()
