from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from backend.app.agents.contracts import (
    CreateAfterSalesCaseArgs,
    CreateHumanTicketArgs,
    CreateShipmentReminderArgs,
    GetAfterSalesStatusArgs,
    GetCustomerOperationArgs,
    GetLogisticsArgs,
    GetOrderArgs,
    SearchPolicyArgs,
    StageOrderFormArgs,
    ToolResult,
)
from backend.app.database import Database
from backend.app.knowledge.service import KnowledgeService
from backend.app.services.order import OrderService
from backend.app.services.after_sales_cases import AfterSalesCaseError, AfterSalesCaseService
from backend.app.services.conversation_context import ConversationContextService
from backend.app.services.customer_operations import (
    CustomerOperationError,
    CustomerOperationService,
)
from backend.app.services.tickets import TicketService
from backend.app.trace_safety import redact


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
        "stage_address_change": StageOrderFormArgs,
        "create_shipment_reminder": CreateShipmentReminderArgs,
        "stage_invoice_application": StageOrderFormArgs,
        "get_invoice": GetCustomerOperationArgs,
        "get_after_sales_status": GetAfterSalesStatusArgs,
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
        conversation_context: ConversationContextService,
        customer_operations: CustomerOperationService | None = None,
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
        self.conversation_context = conversation_context
        self.customer_operations = customer_operations
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
        result = await self.orders.get_order(args.order_id, self.customer_id)
        if result.success:
            await self.conversation_context.set_active_order(
                conversation_id=self.conversation_id,
                customer_id=self.customer_id,
                order_id=args.order_id,
            )
        return result.model_dump(mode="json")

    async def _get_logistics(self, args: GetLogisticsArgs) -> dict[str, Any]:
        result = await self.orders.get_logistics(args.order_id, self.customer_id)
        if result.success:
            await self.conversation_context.set_active_order(
                conversation_id=self.conversation_id,
                customer_id=self.customer_id,
                order_id=args.order_id,
            )
        return result.model_dump(mode="json")

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

    def _require_customer_operations(self) -> CustomerOperationService:
        if self.customer_operations is None:
            raise ToolExecutionError("Customer operation service is unavailable")
        return self.customer_operations

    @staticmethod
    def _operation_data(row) -> dict[str, Any]:
        return {
            "request_no": row.request_no,
            "order_id": row.order_no,
            "request_type": row.request_type,
            "status": row.status,
            "result": row.result_payload,
        }

    async def _stage_address_change(self, args: StageOrderFormArgs) -> dict[str, Any]:
        try:
            row = await self._require_customer_operations().stage_form(
                conversation_id=self.conversation_id,
                customer_id=self.customer_id,
                order_no=args.order_id,
                request_type="address_change",
                idempotency_key=f"agent-run:{self.run_id}:address",
            )
        except CustomerOperationError as exc:
            return ToolResult(
                success=False, error_code=type(exc).__name__, message=str(exc)
            ).model_dump(mode="json")
        return ToolResult(
            success=True,
            message="已创建改址申请",
            data=self._operation_data(row),
        ).model_dump(mode="json")

    async def _create_shipment_reminder(
        self, args: CreateShipmentReminderArgs
    ) -> dict[str, Any]:
        try:
            row = await self._require_customer_operations().request_shipment_reminder(
                conversation_id=self.conversation_id,
                customer_id=self.customer_id,
                order_no=args.order_id,
                idempotency_key=f"agent-run:{self.run_id}:shipment-reminder",
            )
        except CustomerOperationError as exc:
            return ToolResult(
                success=False, error_code=type(exc).__name__, message=str(exc)
            ).model_dump(mode="json")
        return ToolResult(
            success=True,
            message="催发货请求已提交",
            data=self._operation_data(row),
        ).model_dump(mode="json")

    async def _stage_invoice_application(
        self, args: StageOrderFormArgs
    ) -> dict[str, Any]:
        try:
            row = await self._require_customer_operations().stage_form(
                conversation_id=self.conversation_id,
                customer_id=self.customer_id,
                order_no=args.order_id,
                request_type="invoice_application",
                idempotency_key=f"agent-run:{self.run_id}:invoice",
            )
        except CustomerOperationError as exc:
            return ToolResult(
                success=False, error_code=type(exc).__name__, message=str(exc)
            ).model_dump(mode="json")
        return ToolResult(
            success=True,
            message="已创建发票申请",
            data=self._operation_data(row),
        ).model_dump(mode="json")

    async def _get_invoice(self, args: GetCustomerOperationArgs) -> dict[str, Any]:
        rows = await self._require_customer_operations().list(
            self.customer_id,
            order_no=args.order_id,
            request_type="invoice_application",
        )
        if not rows:
            return ToolResult(
                success=False,
                error_code="INVOICE_NOT_FOUND",
                message="未找到该订单的发票申请",
            ).model_dump(mode="json")
        return ToolResult(
            success=True,
            message="发票查询成功",
            data=self._operation_data(rows[0]),
        ).model_dump(mode="json")

    async def _get_after_sales_status(
        self, args: GetAfterSalesStatusArgs
    ) -> dict[str, Any]:
        try:
            case = (
                await self.after_sales_cases.get_case(self.customer_id, args.case_no)
                if args.case_no
                else await self.after_sales_cases.get_latest_for_order(
                    self.customer_id, args.order_id or ""
                )
            )
        except AfterSalesCaseError as exc:
            return ToolResult(
                success=False,
                error_code="AFTER_SALES_NOT_FOUND",
                message=str(exc),
            ).model_dump(mode="json")
        return ToolResult(
            success=True,
            message="售后进度查询成功",
            data={
                "case_no": case.case_no,
                "order_id": case.order_no,
                "order_item_id": case.order_item_no,
                "case_type": case.case_type,
                "status": case.status,
                "evidence_required": case.evidence_required,
                "problem_discovered_at_required": case.problem_discovered_at_required,
                "evidence_deadline_at": case.evidence_deadline_at,
                "deadline_status": case.deadline_status,
            },
        ).model_dump(mode="json")
