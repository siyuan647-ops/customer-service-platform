from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from backend.app.agents.after_sales import AfterSalesAgent
from backend.app.agents.contracts import (
    AgentOutcome,
    AgentRequest,
    ProductCategory,
    ProductTag,
    SupervisorPlan,
    WorkflowResult,
)
from backend.app.agents.tools import ToolRuntime, TraceRecorder
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.knowledge.service import KnowledgeService
from backend.app.orchestration.workflow import CustomerServiceWorkflow
from backend.app.services.order import OrderService
from backend.app.services.after_sales_cases import AfterSalesCaseService
from backend.app.services.conversation_context import ConversationContextService
from backend.app.services.customer_operations import CustomerOperationService
from backend.app.services.tickets import TicketService
from backend.app.security.circuit_breaker import CircuitBreaker, optional_guard


DeltaWriter = Callable[[str], Awaitable[None]]
_ORDER_PATTERN = re.compile(r"ORD-\d{8}-\d{3}", re.IGNORECASE)
_ORDER_ITEM_PATTERN = re.compile(r"ITEM-\d{8}-\d{3}-\d{2}", re.IGNORECASE)
_AFTER_SALES_TERMS = (
    "退货",
    "退款",
    "换货",
    "补发",
    "保修",
    "维修",
    "售后",
    "破损",
    "损坏",
    "坏了",
    "变质",
    "质量",
    "故障",
    "缺件",
    "少件",
    "无法使用",
    "不能正常使用",
)
_MOCK_PRODUCT_CATEGORIES: dict[ProductCategory, tuple[str, ...]] = {
    ProductCategory.FOOD_FRESH: ("食品生鲜", "生鲜食品", "生鲜"),
    ProductCategory.CLOTHING: ("服饰鞋包", "服饰", "衣服", "鞋", "袜子", "内衣"),
    ProductCategory.BEAUTY: ("美妆个护", "美妆", "护肤", "化妆品"),
    ProductCategory.DIGITAL: ("数码电器", "数码", "手机", "电脑"),
    ProductCategory.HOME_APPLIANCE: ("家居家电", "家电", "家具"),
    ProductCategory.MATERNAL_CHILD: ("母婴用品", "母婴"),
    ProductCategory.SPORTS: ("运动户外", "运动", "户外"),
    ProductCategory.BOOKS: ("图书文娱", "图书", "书籍"),
    ProductCategory.PET: ("宠物用品", "宠物"),
    ProductCategory.HEALTH: ("医药保健", "医药", "保健"),
    ProductCategory.VIRTUAL: ("虚拟商品", "虚拟", "充值", "卡券"),
}
_MOCK_PRODUCT_TAGS: dict[ProductTag, tuple[str, ...]] = {
    ProductTag.FRESH: ("生鲜食品", "生鲜"),
    ProductTag.CUSTOMIZED: ("定制商品", "定制"),
    ProductTag.VIRTUAL: ("虚拟商品", "虚拟", "充值", "卡券"),
    ProductTag.PERSONAL: ("贴身用品", "贴身", "内衣", "内裤", "袜子", "泳装"),
}


def _extract_text_delta(event: Any) -> str:
    if getattr(event, "type", None) != "raw_response_event":
        return ""
    data = getattr(event, "data", None)
    if getattr(data, "type", "") in {"response.output_text.delta", "output_text_delta"}:
        return str(getattr(data, "delta", ""))
    return ""


class SupervisorAgent:
    def __init__(
        self,
        settings: Settings,
        database: Database | None,
        knowledge: KnowledgeService,
        orders: OrderService,
        tickets: TicketService,
        after_sales: AfterSalesAgent,
        after_sales_cases: AfterSalesCaseService,
        conversation_context: ConversationContextService | None = None,
        customer_operations: CustomerOperationService | None = None,
        model_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.settings = settings
        self.model_breaker = model_breaker
        self.database = database
        self.knowledge = knowledge
        self.orders = orders
        self.tickets = tickets
        self.after_sales_cases = after_sales_cases
        self.conversation_context = conversation_context or ConversationContextService(database)
        self.customer_operations = customer_operations
        self.workflow = CustomerServiceWorkflow(after_sales)

    async def run(self, request: AgentRequest, on_delta: DeltaWriter) -> AgentOutcome:
        recorder = TraceRecorder()
        recorder.emit(
            "run_started",
            run_id=str(request.run_id),
            conversation_id=str(request.conversation_id),
            model=self.settings.kimi_model if self.settings.agent_mode == "live" else "mock",
            prompt_length=len(request.prompt),
            architecture="supervisor_workflow_after_sales",
        )
        runtime = ToolRuntime(
            database=self.database,
            run_id=request.run_id,
            conversation_id=request.conversation_id,
            customer_id=request.customer_id,
            recorder=recorder,
            timeout_seconds=self.settings.tool_timeout_seconds,
            max_calls=self.settings.agent_max_tool_calls,
            knowledge=self.knowledge,
            orders=self.orders,
            tickets=self.tickets,
            after_sales_cases=self.after_sales_cases,
            conversation_context=self.conversation_context,
            customer_operations=self.customer_operations,
        )
        try:
            recorder.emit("supervisor_planning_started")
            plan = await self._plan(request)
            plan = await self._apply_conversation_context(plan, request, recorder)
            recorder.emit("supervisor_plan_completed", plan=plan.model_dump(mode="json"))
            result = await self.workflow.execute(plan=plan, prompt=request.prompt, runtime=runtime)
            recorder.emit("workflow_completed", result=result.model_dump(mode="json"))

            if self.settings.agent_mode == "mock":
                final = self._compose_mock(result)
                for index in range(0, len(final), 12):
                    await on_delta(final[index : index + 12])
                    await asyncio.sleep(0)
            else:
                final = await self._compose_live(request, result, on_delta)
            if not final.strip():
                raise RuntimeError("Supervisor returned an empty response")
            recorder.emit("run_completed", tool_call_count=runtime.call_count)
            return AgentOutcome(final, runtime.call_count, recorder.events)
        except Exception as exc:
            recorder.emit("run_failed", error_type=type(exc).__name__, error=str(exc))
            setattr(exc, "safe_trace", recorder.events)
            setattr(exc, "tool_call_count", runtime.call_count)
            raise

    async def _plan(self, request: AgentRequest) -> SupervisorPlan:
        if self.settings.agent_mode == "mock":
            return self._plan_mock(request)
        plan = await self._plan_live(request)
        return self._normalize_plan(plan, request)

    async def _apply_conversation_context(
        self,
        plan: SupervisorPlan,
        request: AgentRequest,
        recorder: TraceRecorder,
    ) -> SupervisorPlan:
        if plan.intent not in {
            "order_query",
            "logistics_query",
            "after_sales",
            "address_change",
            "shipment_reminder",
            "invoice_apply",
            "invoice_query",
            "after_sales_status",
        }:
            return plan
        context = await self.conversation_context.get(
            conversation_id=request.conversation_id,
            customer_id=request.customer_id,
        )
        updates: dict[str, Any] = {}
        if plan.order_id is None and context.active_order_id is not None:
            updates["order_id"] = context.active_order_id
            updates["missing_information"] = [
                item for item in plan.missing_information if item != "订单号"
            ]
            if plan.order_item_id is None and context.active_order_item_id is not None:
                updates["order_item_id"] = context.active_order_item_id
            recorder.emit(
                "conversation_context_applied",
                fields=["active_order_id"],
            )
        elif (
            plan.order_id is not None
            and plan.order_id == context.active_order_id
            and plan.order_item_id is None
            and context.active_order_item_id is not None
        ):
            updates["order_item_id"] = context.active_order_item_id
            recorder.emit(
                "conversation_context_applied",
                fields=["active_order_item_id"],
            )
        return plan.model_copy(update=updates) if updates else plan

    @staticmethod
    def _mock_product_category(prompt: str) -> ProductCategory | None:
        for category, aliases in _MOCK_PRODUCT_CATEGORIES.items():
            if any(alias in prompt for alias in aliases):
                return category
        return None

    @staticmethod
    def _mock_product_tags(prompt: str) -> list[ProductTag]:
        return [
            tag
            for tag, aliases in _MOCK_PRODUCT_TAGS.items()
            if any(alias in prompt for alias in aliases)
        ]

    @staticmethod
    def _mock_policy_category(
        prompt: str,
    ) -> Literal["refund", "shipping", "after_sale", "general"]:
        if any(term in prompt for term in ("物流", "快递", "配送", "发货", "签收", "拒收")):
            return "shipping"
        if any(term in prompt for term in ("退款", "退货", "七天无理由", "价保", "差价")):
            return "refund"
        if any(term in prompt for term in ("售后", "破损", "质量", "补发", "保修", "维修")):
            return "after_sale"
        return "general"

    def _plan_mock(self, request: AgentRequest) -> SupervisorPlan:
        prompt = request.prompt
        lowered = prompt.casefold()
        current_match = _ORDER_PATTERN.search(prompt)
        context_match = current_match
        if context_match is None:
            for turn in reversed(request.history):
                context_match = _ORDER_PATTERN.search(turn.content)
                if context_match:
                    break
        contextual_order_id = context_match.group(0).upper() if context_match else None
        product_category = self._mock_product_category(prompt)
        product_tags = self._mock_product_tags(prompt)
        policy_category = self._mock_policy_category(prompt)
        history_text = "\n".join(turn.content for turn in request.history)

        if any(
            phrase in prompt for phrase in ("确认提交", "确认申请", "提交售后申请")
        ) and any(
            marker in history_text for marker in ("可以提交售后申请", "确认提交")
        ):
            intent = "after_sales_confirm"
        elif any(word in lowered for word in ("人工", "客服介入", "工单")):
            intent = "human_handoff"
        elif (
            "售后" in lowered
            or any(
                word in lowered
                for word in (
                    "退款申请",
                    "换货申请",
                    "补发申请",
                    "维修申请",
                    "退款",
                    "换货",
                    "补发",
                    "维修",
                )
            )
        ) and any(word in lowered for word in ("进度", "状态", "处理到哪", "结果")):
            intent = "after_sales_status"
        elif "地址" in lowered and any(
            word in lowered
            for word in (
                "修改",
                "更改",
                "改成",
                "换成",
                "换一个",
                "改一下",
                "填错",
                "错了",
            )
        ):
            intent = "address_change"
        elif any(
            word in lowered
            for word in ("催发货", "催单", "尽快发货", "催一下", "催促", "提醒仓库")
        ):
            intent = "shipment_reminder"
        elif any(word in lowered for word in ("发票", "开票")) and any(
            word in lowered
            for word in (
                "查询",
                "查一下",
                "进度",
                "状态",
                "下载",
                "开好",
                "查看",
                "显示",
            )
        ):
            intent = "invoice_query"
        elif any(word in lowered for word in ("发票", "开票")) and any(
            word in lowered
            for word in ("申请", "开票", "补开", "开发票", "要发票", "开具")
        ):
            intent = "invoice_apply"
        elif (
            any(
                word in lowered
                for word in ("物流", "快递", "配送", "包裹", "运单", "送达")
            )
            or ("到哪" in lowered and "订单处理" not in lowered)
        ) and not any(word in lowered for word in ("政策", "规则")):
            intent = "logistics_query"
        elif any(word in lowered for word in _AFTER_SALES_TERMS) and (
            contextual_order_id
            or any(marker in lowered for marker in ("我的", "我买", "这件", "这个商品"))
        ):
            intent = "after_sales"
        elif any(word in lowered for word in ("政策", "规则", "七天无理由", *_AFTER_SALES_TERMS)):
            intent = "policy_query"
        elif current_match or "订单" in lowered:
            intent = "order_query"
        else:
            intent = "general"

        requires_order = intent in {
            "order_query",
            "logistics_query",
            "after_sales",
            "address_change",
            "shipment_reminder",
            "invoice_apply",
            "invoice_query",
            "after_sales_status",
        }
        order_id = contextual_order_id if requires_order else None
        return SupervisorPlan(
            intent=intent,
            order_id=order_id,
            order_item_id=(
                item_match.group(0).upper()
                if (item_match := _ORDER_ITEM_PATTERN.search(prompt))
                else None
            ),
            product_category=product_category,
            product_tags=product_tags,
            policy_category=policy_category,
            need_order_data=requires_order,
            need_policy_evidence=intent in {"policy_query", "after_sales"},
            need_after_sales_decision=intent == "after_sales",
            missing_information=["订单号"] if requires_order and not order_id else [],
        )

    @staticmethod
    def _normalize_plan(plan: SupervisorPlan, request: AgentRequest) -> SupervisorPlan:
        prompt = request.prompt
        match = _ORDER_PATTERN.search(prompt)
        order_id = match.group(0).upper() if match else None
        if order_id is None and plan.order_id and _ORDER_PATTERN.fullmatch(plan.order_id):
            history_text = "\n".join(turn.content for turn in request.history)
            if plan.order_id.casefold() in history_text.casefold():
                order_id = plan.order_id.upper()
        requires_order = plan.intent in {
            "order_query",
            "logistics_query",
            "after_sales",
            "address_change",
            "shipment_reminder",
            "invoice_apply",
            "invoice_query",
            "after_sales_status",
        }
        item_match = _ORDER_ITEM_PATTERN.search(prompt)
        order_item_id = item_match.group(0).upper() if item_match else None
        if order_item_id is None and plan.order_item_id and _ORDER_ITEM_PATTERN.fullmatch(
            plan.order_item_id
        ):
            history_text = "\n".join(turn.content for turn in request.history)
            if plan.order_item_id.casefold() in history_text.casefold():
                order_item_id = plan.order_item_id.upper()
        updates: dict[str, Any] = {
            "order_id": order_id,
            "order_item_id": order_item_id,
            "need_order_data": requires_order,
            "need_policy_evidence": plan.intent in {"policy_query", "after_sales"},
            "need_after_sales_decision": plan.intent == "after_sales",
        }
        if requires_order and not order_id:
            updates["missing_information"] = ["订单号"]
        else:
            # Planner only decides how to route the request. Evidence, problem time,
            # packaging photos and other case materials must be determined after
            # order/policy lookup and collected through the after-sales case UI.
            updates["missing_information"] = []
        return plan.model_copy(update=updates)

    async def _plan_live(self, request: AgentRequest) -> SupervisorPlan:
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
        model = OpenAIChatCompletionsModel(model=self.settings.kimi_model, openai_client=client)
        captured: list[SupervisorPlan] = []

        @function_tool(strict_mode=False)
        async def submit_plan(
            intent: Literal[
                "order_query",
                "logistics_query",
                "policy_query",
                "after_sales",
                "after_sales_confirm",
                "address_change",
                "shipment_reminder",
                "invoice_apply",
                "invoice_query",
                "after_sales_status",
                "human_handoff",
                "general",
            ],
            order_id: str = "",
            order_item_id: str = "",
            product_category: ProductCategory | None = None,
            product_tags: list[ProductTag] | None = None,
            policy_category: Literal["refund", "shipping", "after_sale", "general"] = "general",
            need_order_data: bool = False,
            need_policy_evidence: bool = False,
            need_after_sales_decision: bool = False,
            missing_information: list[str] | None = None,
        ) -> str:
            """提交且仅提交一次经过分析的客服执行计划。

            Args:
                intent: 客服请求的执行意图。
                order_id: 用户当前消息或历史消息中出现的订单号；无法确定时传空字符串。
                order_item_id: 用户当前消息或历史消息中出现的商品项编号；无法确定时传空字符串。
                product_category: 只能使用声明的候选分类；无法确定时必须传 null。
                product_tags: 用户明确提到的政策特征标签；无法确定时传空列表。
                policy_category: 政策意图分类；无法确定时必须传 general。
                need_order_data: 是否需要查询订单数据。
                need_policy_evidence: 是否需要检索政策证据。
                need_after_sales_decision: 是否需要售后专家判断。
                missing_information: 执行前仍需用户补充的信息。
            """
            captured.append(
                SupervisorPlan(
                    intent=intent,
                    order_id=order_id or None,
                    order_item_id=order_item_id or None,
                    product_category=product_category,
                    product_tags=product_tags or [],
                    policy_category=policy_category,
                    need_order_data=need_order_data,
                    need_policy_evidence=need_policy_evidence,
                    need_after_sales_decision=need_after_sales_decision,
                    missing_information=missing_information or [],
                )
            )
            return "计划已接收"

        agent = Agent(
            name="supervisor_planner",
            instructions=(
                "你是电商客服编排器，不回答用户问题，必须且只能调用一次 submit_plan。"
                "订单/物流查询需要订单数据；具体订单的退款、退货、换货、补发、保修、"
                "破损或质量判断属于 after_sales；只询问规则属于 policy_query；明确要求人工"
                "属于 human_handoff。用户在上一轮获得可申请结论后明确回复确认提交，属于"
                " after_sales_confirm。修改收货地址属于 address_change；催发货属于"
                " shipment_reminder；申请或补开发票属于 invoice_apply；查询发票属于"
                " invoice_query；查询已有售后申请进度属于 after_sales_status。不得虚构"
                "订单号或商品分类。商品分类只能选择工具声明的"
                "候选值；product_category 无法确定时必须传 null。policy_category 只能选择"
                "refund、shipping、after_sale、general；无法确定时必须传 general。订单商品项"
                "编号只能来自对话；政策标签不确定时传空列表。missing_information 只允许"
                "填写执行前缺失的订单号；照片、视频、快递面单、商品包装、问题描述、问题"
                "发现时间等售后材料不得填入 missing_information，必须继续进入订单查询、"
                "政策检索和售后判断，由售后材料窗口收集。"
            ),
            model=model,
            model_settings=ModelSettings(
                max_tokens=self.settings.supervisor_plan_max_tokens,
                extra_body={"thinking": {"type": "disabled"}},
            ),
            tools=[submit_plan],
            tool_use_behavior="stop_on_first_tool",
        )
        payload = json.dumps(
            {
                "conversation_history": [turn.model_dump(mode="json") for turn in request.history],
                "current_user_message": request.prompt,
            },
            ensure_ascii=False,
        )
        try:
            async with optional_guard(self.model_breaker):
                async with asyncio.timeout(self.settings.agent_timeout_seconds):
                    await Runner.run(agent, input=payload, max_turns=2)
            if not captured:
                raise RuntimeError("Supervisor planner did not submit a plan")
            return captured[0]
        finally:
            await client.close()

    @staticmethod
    def _compose_mock(result: WorkflowResult) -> str:
        if result.status == "need_more_information" and not result.after_sales:
            return f"请提供{'、'.join(result.required_information)}，我才能继续处理。"
        if result.status in {"not_found", "error"}:
            return result.message or "暂时无法确认相关信息，请稍后重试。"
        if result.after_sales_case and not result.after_sales:
            case = result.after_sales_case
            if case.status == "WAITING_MATERIALS":
                return (
                    f"售后申请 {case.case_no} 正在等待材料，请在材料窗口填写问题类型，"
                    "并按提示上传照片或视频后提交。"
                )
            status_label = {
                "DRAFT": "等待补充申请信息",
                "SUBMITTED": "已提交，等待客服审核",
                "UNDER_REVIEW": "人工审核中",
                "APPROVED": "审核已通过，等待执行",
                "EXECUTING": "正在执行售后处理",
                "CANCEL_PENDING": "正在取消订单",
                "REFUND_PENDING": "正在退款",
                "COMPLETED": "已完成",
                "REJECTED": "审核未通过",
                "EXECUTION_FAILED": "执行失败，等待客服重试",
            }.get(case.status, case.status)
            return f"售后申请 {case.case_no} 当前进度：{status_label}。"
        if result.ticket_id and not result.after_sales:
            return f"已为你创建人工工单，工单号：{result.ticket_id}。客服会尽快处理。"
        if result.customer_operation:
            operation = result.customer_operation
            if operation.request_type == "address_change":
                return (
                    f"订单 {operation.order_id} 可以申请修改收货地址，已创建申请"
                    f" {operation.request_no}。请在地址表单填写并确认新地址。"
                )
            if operation.request_type == "shipment_reminder":
                return (
                    f"已为订单 {operation.order_id} 提交催发货请求"
                    f"（{operation.request_no}），24小时内不会重复催单。"
                )
            if operation.status == "DRAFT":
                return (
                    f"已为订单 {operation.order_id} 创建发票申请"
                    f" {operation.request_no}，请在发票表单填写开票信息。"
                )
            download_url = operation.result.get("download_url")
            return (
                f"订单 {operation.order_id} 的发票状态为 {operation.status}。"
                + (f"下载地址：{download_url}" if download_url else "")
            )
        if result.order:
            if result.after_sales:
                return SupervisorAgent._compose_after_sales(result)
            order = result.order
            products = "、".join(
                f"{item.product_name}×{item.quantity}" for item in order.items
            ) or "暂无商品明细"
            return (
                f"订单 {order.order_id} 当前状态为 {order.status}，商品："
                f"{products}，金额 {order.amount} {order.currency}。"
            )
        if result.logistics:
            logistics = result.logistics
            if not logistics.shipments:
                return f"订单 {logistics.order_id} 暂无物流信息。"
            details = "；".join(
                f"{item.carrier or '商家'}：{item.tracking_status}"
                for item in logistics.shipments
            )
            return f"订单 {logistics.order_id} 的物流状态：{details}。"
        if result.evidence:
            return "；".join(
                f"{item.title}：{item.content}【来源：{item.source_filename}，{item.section}】"
                for item in result.evidence[:3]
            )
        return "我可以帮你查询订单、物流和售后政策，也可以处理售后判断或创建人工工单。"

    @staticmethod
    def _compose_after_sales(result: WorkflowResult) -> str:
        decision = result.after_sales
        assert decision is not None
        prefix = {
            "eligible": "根据订单事实和政策证据，可以提交售后申请。",
            "ineligible": "根据当前订单事实和政策证据，暂不符合该售后条件。",
            "need_more_information": "还需要补充信息后才能判断。",
            "policy_conflict": "当前政策证据存在冲突，需要人工复核。",
        }[decision.decision]
        if decision.reason_code == "AFTER_SALES_CASE_EXISTS":
            prefix = ""
        parts = [part for part in (prefix, decision.reason) if part]
        if decision.required_information:
            parts.append("请补充：" + "、".join(decision.required_information) + "。")
        if decision.recommended_actions:
            parts.append("建议：" + "；".join(decision.recommended_actions) + "。")
        seen: set[tuple[str, str, str]] = set()
        for reference in decision.policy_references:
            key = (reference.source_filename, reference.section, reference.title)
            if key not in seen:
                seen.add(key)
                parts.append(f"【来源：{reference.source_filename}，{reference.section}】")
        if decision.should_handoff:
            if result.ticket_id:
                parts.append(f"已自动创建人工工单，工单号：{result.ticket_id}。")
            else:
                parts.append("建议转人工客服进一步处理。")
        elif (
            decision.reason_code != "AFTER_SALES_CASE_EXISTS"
            and result.after_sales_case
            and result.after_sales_case.status in {
            "WAITING_MATERIALS",
            "DRAFT",
            }
        ):
            parts.append(
                f"已建立待补材料的售后申请 {result.after_sales_case.case_no}，"
                "请在材料窗口填写问题类型并上传照片或视频，确认无误后提交。"
            )
        elif decision.decision == "eligible":
            parts.append("如需创建售后申请，请回复“确认提交”。")
        return "".join(parts)

    async def _compose_live(
        self,
        request: AgentRequest,
        result: WorkflowResult,
        on_delta: DeltaWriter,
    ) -> str:
        from agents import Agent, ModelSettings, Runner, set_tracing_disabled
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
        from openai import AsyncOpenAI

        set_tracing_disabled(True)
        client = AsyncOpenAI(
            api_key=self.settings.kimi_api_key,
            base_url=self.settings.kimi_base_url,
            timeout=self.settings.agent_timeout_seconds,
            max_retries=0,
        )
        model = OpenAIChatCompletionsModel(model=self.settings.kimi_model, openai_client=client)
        agent = Agent(
            name="supervisor_responder",
            instructions=(
                "你是电商客服 Supervisor。根据已经执行并校验的工作流结果生成简洁中文回复。"
                "不得添加结果中不存在的订单或政策事实。政策引用只能使用 evidence 或"
                "after_sales.policy_references 中的来源，格式为【来源：文件名，章节】。"
                "如果 required_information 非空，应清楚追问；should_handoff 为 true 时建议"
                "转人工，但只有 ticket_id 存在时才能声称工单已创建。不要透露内部编排。"
                "只要 after_sales_case.status 是 WAITING_MATERIALS 或 DRAFT，就必须准确给出"
                "申请单号，并要求用户在材料窗口填写问题类型、"
                "上传照片或视频后确认提交；不得声称已经提交。"
                "WAITING_MATERIALS 表示正在等待用户补充并提交材料，SUBMITTED 才表示已经提交。"
                "如果 reason_code 是 AFTER_SALES_CASE_EXISTS，只能引导用户继续处理已有申请，"
                "不得创建或暗示需要创建新的售后申请，也不得声称当前材料窗口一定会显示。"
            ),
            model=model,
            model_settings=ModelSettings(
                max_tokens=self.settings.supervisor_response_max_tokens,
                extra_body={"thinking": {"type": "disabled"}},
            ),
        )
        payload = json.dumps(
            {
                "current_user_message": request.prompt,
                "workflow_result": result.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
        chunks: list[str] = []
        try:
            async with optional_guard(self.model_breaker):
                async with asyncio.timeout(self.settings.agent_timeout_seconds):
                    streamed = Runner.run_streamed(agent, input=payload, max_turns=self.settings.agent_max_turns)
                    async for event in streamed.stream_events():
                        delta = _extract_text_delta(event)
                        if delta:
                            chunks.append(delta)
                            await on_delta(delta)
            final = str(streamed.final_output or "")
            if not chunks and final:
                await on_delta(final)
            return final
        finally:
            await client.close()
