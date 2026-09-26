from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from backend.app.agents.contracts import (
    CreateAfterSalesCaseArgs,
    CreateHumanTicketArgs,
    GetLogisticsArgs,
    GetOrderArgs,
    SearchPolicyArgs,
    ToolResult,
)
from backend.app.database import Database
from backend.app.knowledge.service import KnowledgeService
from backend.app.services.order import OrderService
from backend.app.services.after_sales_cases import AfterSalesCaseError, AfterSalesCaseService
from backend.app.services.tickets import TicketService
from kimi_agent_spike.safe_trace import redact


class ToolExecutionError(RuntimeError):
    pass


class TraceRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: str, **payload: Any) -> None:
        self.events.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": event,
                **redact(payload),
            }
        )


class ToolRuntime:
    """Application-owned tool executor with validation, limits and safe traces."""

    argument_models: dict[str, type[BaseModel]] = {
        "get_order": GetOrderArgs,
        "get_logistics": GetLogisticsArgs,
        "search_policy": SearchPolicyArgs,
        "create_human_ticket": CreateHumanTicketArgs,
        "create_after_sales_case": CreateAfterSalesCaseArgs,
    }

    def __init__(
        self,
        *,
        database: Database,
        run_id: uuid.UUID,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        recorder: TraceRecorder,
        timeout_seconds: float,
        max_calls: int,
        knowledge: KnowledgeService,
        orders: OrderService,
        tickets: TicketService,
        after_sales_cases: AfterSalesCaseService,
    ) -> None:
        self.database = database
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.customer_id = customer_id
        self.recorder = recorder
        self.timeout_seconds = timeout_seconds
        self.max_calls = max_calls
        self.knowledge = knowledge
        self.orders = orders
        self.tickets = tickets
        self.after_sales_cases = after_sales_cases
        self.started_at = datetime.now(UTC)
        self.call_count = 0

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.argument_models:
            raise ToolExecutionError(f"Unsupported tool: {name}")
        if self.call_count >= self.max_calls:
            raise ToolExecutionError("Maximum tool call count exceeded")
        self.call_count += 1
        try:
            parsed = self.argument_models[name].model_validate(arguments)
        except ValidationError as exc:
            self.recorder.emit("tool_validation_failed", tool_name=name, error=str(exc))
            raise ToolExecutionError(f"Invalid arguments for {name}") from exc

        self.recorder.emit("tool_started", tool_name=name, arguments=parsed.model_dump())
        try:
            async with asyncio.timeout(self.timeout_seconds):
                handler = getattr(self, f"_{name}")
                result = ToolResult.model_validate(await handler(parsed))
        except TimeoutError as exc:
            self.recorder.emit("tool_failed", tool_name=name, error_type="TimeoutError")
            raise ToolExecutionError(f"Tool {name} timed out") from exc
        self.recorder.emit("tool_completed", tool_name=name, result=result.model_dump())
        return result.model_dump(mode="json")

    async def _get_order(self, args: GetOrderArgs) -> dict[str, Any]:
        return (
            await self.orders.get_order(args.order_id, self.customer_id)
        ).model_dump(mode="json")

    async def _get_logistics(self, args: GetLogisticsArgs) -> dict[str, Any]:
        return (
            await self.orders.get_logistics(args.order_id, self.customer_id)
        ).model_dump(mode="json")

    async def _search_policy(self, args: SearchPolicyArgs) -> dict[str, Any]:
        matches = await self.knowledge.search(
            args.query,
            top_k=args.top_k,
            policy_category=args.category,
            product_category=args.product_category,
        )
        return {
            "success": True,
            "error_code": None,
            "message": "政策检索完成",
            "data": {"items": matches},
        }

    async def _create_human_ticket(self, args: CreateHumanTicketArgs) -> dict[str, Any]:
        result = await self.tickets.create(
            run_id=self.run_id,
            conversation_id=self.conversation_id,
            customer_id=self.customer_id,
            request=args,
        )
        return result.model_dump(mode="json")

    async def _create_after_sales_case(
        self, args: CreateAfterSalesCaseArgs
    ) -> dict[str, Any]:
        if not args.confirmed:
            return ToolResult(
                success=False,
                error_code="CONFIRMATION_REQUIRED",
                message="创建售后申请前需要用户明确确认",
            ).model_dump(mode="json")
        try:
            case = await self.after_sales_cases.confirm_latest_case(
                conversation_id=self.conversation_id,
                customer_id=self.customer_id,
            )
        except AfterSalesCaseError as exc:
            return ToolResult(
                success=False,
                error_code="AFTER_SALES_CASE_NOT_READY",
                message=str(exc),
            ).model_dump(mode="json")
        return ToolResult(
            success=True,
            error_code=None,
            message="售后申请已创建",
            data={
                "case_no": case.case_no,
                "order_id": case.order_no,
                "order_item_id": case.order_item_no,
                "case_type": case.case_type,
                "status": case.status,
                "evidence_required": case.evidence_required,
                "problem_discovered_at_required": (
                    case.problem_discovered_at_required
                ),
            },
        ).model_dump(mode="json")
