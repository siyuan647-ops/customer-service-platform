from __future__ import annotations

import asyncio
import json
from typing import Literal

from backend.app.agents.contracts import (
    AfterSalesResult,
    OrderFacts,
    OrderItemFacts,
    PolicyDeadlineFacts,
    PolicyEvidence,
    PolicyReference,
)
from backend.app.config import Settings


_QUALITY_TERMS = ("破损", "损坏", "坏了", "变质", "质量", "故障", "少件", "缺件")
_REFUND_TERMS = ("退款", "退货", "换货", "补发", "保修")


class AfterSalesAgent:
    """Makes a typed after-sales decision from facts and retrieved evidence only."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def decide(
        self,
        *,
        user_request: str,
        order: OrderFacts,
        order_item: OrderItemFacts,
        evidence: list[PolicyEvidence],
        policy_deadline: PolicyDeadlineFacts | None = None,
    ) -> AfterSalesResult:
        if self.settings.agent_mode == "mock":
            return self._decide_mock(
                user_request, order, order_item, evidence, policy_deadline
            )
        return await self._decide_live(
            user_request, order, order_item, evidence, policy_deadline
        )

    @staticmethod
    def _references(evidence: list[PolicyEvidence]) -> list[PolicyReference]:
        return [
            PolicyReference(
                title=item.title,
                section=item.section,
                source_filename=item.source_filename,
                quote=item.content,
            )
            for item in evidence[:3]
        ]

    def _decide_mock(
        self,
        user_request: str,
        order: OrderFacts,
        order_item: OrderItemFacts,
        evidence: list[PolicyEvidence],
        policy_deadline: PolicyDeadlineFacts | None,
    ) -> AfterSalesResult:
        if not evidence:
            return AfterSalesResult(
                decision="need_more_information",
                reason_code="POLICY_EVIDENCE_MISSING",
                reason="没有检索到足以支持售后判断的政策证据。",
                required_information=["适用的售后政策"],
                risk_level="medium",
                should_handoff=True,
            )

        references = self._references(evidence)
        if any(term in user_request for term in _QUALITY_TERMS):
            return AfterSalesResult(
                decision="eligible",
                reason_code="QUALITY_OR_DAMAGE_CLAIM",
                reason="订单商品存在质量、破损或变质诉求，政策证据支持提交售后申请。",
                required_information=["商品问题照片", "外包装照片"],
                recommended_actions=["提交退款、换货或补发申请", "保留商品及外包装"],
                policy_references=references,
                risk_level="low",
                should_handoff=False,
            )

        combined_evidence = "\n".join(item.content for item in evidence)
        if (
            any(term in user_request for term in ("七天无理由", "无理由"))
            and "不支持七天无理由" in combined_evidence
        ):
            return AfterSalesResult(
                decision="ineligible",
                reason_code="CATEGORY_EXCLUDED_FROM_NO_REASON_RETURN",
                reason="当前商品分类不支持七天无理由退货；质量问题仍可按对应政策申请售后。",
                recommended_actions=["如商品存在质量问题，请补充问题描述和照片"],
                policy_references=references,
                risk_level="low",
                should_handoff=False,
            )

        if any(term in user_request for term in _REFUND_TERMS):
            return AfterSalesResult(
                decision="need_more_information",
                reason_code="CLAIM_DETAILS_REQUIRED",
                reason="已有相关政策，但仍需商品问题和申请原因才能作出具体判断。",
                required_information=["售后原因", "商品当前状态", "相关凭证"],
                policy_references=references,
                risk_level="low",
                should_handoff=False,
            )

        return AfterSalesResult(
            decision="need_more_information",
            reason_code="AFTER_SALES_INTENT_UNCLEAR",
            reason="售后诉求不够明确。",
            required_information=["希望办理退款、换货、补发还是保修"],
            policy_references=references,
            risk_level="low",
            should_handoff=False,
        )

    async def _decide_live(
        self,
        user_request: str,
        order: OrderFacts,
        order_item: OrderItemFacts,
        evidence: list[PolicyEvidence],
        policy_deadline: PolicyDeadlineFacts | None,
    ) -> AfterSalesResult:
        if not self.settings.kimi_api_key:
            raise RuntimeError("KIMI_API_KEY is required when AGENT_MODE=live")
        from agents import Agent, ModelSettings, Runner, function_tool, set_tracing_disabled
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
        from openai import AsyncOpenAI

        set_tracing_disabled(True)
        client = AsyncOpenAI(
            api_key=self.settings.kimi_api_key,
            base_url=self.settings.kimi_base_url,
            timeout=self.settings.agent_timeout_seconds,
            max_retries=0,
        )
        model = OpenAIChatCompletionsModel(
            model=self.settings.kimi_model, openai_client=client
        )
        captured: list[AfterSalesResult] = []
        evidence_by_id = {item.chunk_id: item for item in evidence}

        @function_tool(strict_mode=False)
        async def submit_after_sales_decision(
            decision: Literal[
                "eligible",
                "ineligible",
                "need_more_information",
                "policy_conflict",
            ],
            reason_code: str,
            reason: str,
            required_information: list[str] | None = None,
            recommended_actions: list[str] | None = None,
            policy_reference_chunk_ids: list[str] | None = None,
            risk_level: Literal["low", "medium", "high"] = "low",
            should_handoff: bool = False,
        ) -> str:
            """提交且仅提交一次结构化售后判定。"""
            references = [
                PolicyReference(
                    title=evidence_by_id[chunk_id].title,
                    section=evidence_by_id[chunk_id].section,
                    source_filename=evidence_by_id[chunk_id].source_filename,
                    quote=evidence_by_id[chunk_id].content,
                )
                for chunk_id in (policy_reference_chunk_ids or [])
                if chunk_id in evidence_by_id
            ]
            captured.append(
                AfterSalesResult(
                    decision=decision,
                    reason_code=reason_code,
                    reason=reason,
                    required_information=required_information or [],
                    recommended_actions=recommended_actions or [],
                    policy_references=references,
                    risk_level=risk_level,
                    should_handoff=should_handoff,
                )
            )
            return "售后判定已接收"

        agent = Agent(
            name="after_sales_agent",
            instructions=(
                "你是售后判定专家，只能使用输入中的订单事实和政策证据。"
                "不得补充未提供的事实、不得输出面向用户的话术，必须且只能调用一次"
                " submit_after_sales_decision。"
                "eligible 或 ineligible 必须给出输入中存在的政策引用；证据不足时返回"
                "need_more_information，政策互相冲突时返回 policy_conflict。引用政策时只"
                "提交输入中的 chunk_id，不要复制或改写政策正文。policy_deadline 是后端"
                "已经计算完成的可信事实，不得自行重新计算或用用户陈述覆盖。"
            ),
            model=model,
            model_settings=ModelSettings(
                extra_body={"thinking": {"type": "disabled"}}
            ),
            tools=[submit_after_sales_decision],
            tool_use_behavior="stop_on_first_tool",
        )
        payload = json.dumps(
            {
                "user_request": user_request,
                "order": order.model_dump(mode="json"),
                "order_item": order_item.model_dump(mode="json"),
                "policy_evidence": [item.model_dump(mode="json") for item in evidence],
                "policy_deadline": (
                    policy_deadline.model_dump(mode="json")
                    if policy_deadline is not None
                    else None
                ),
            },
            ensure_ascii=False,
        )
        try:
            async with asyncio.timeout(self.settings.agent_timeout_seconds):
                await Runner.run(agent, input=payload, max_turns=2)
            if not captured:
                raise RuntimeError("After-sales agent did not submit a decision")
            return captured[0]
        finally:
            await client.close()
