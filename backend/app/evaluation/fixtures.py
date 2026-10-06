from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Protocol

from backend.app.agents.contracts import (
    ConversationContext,
    LogisticsFacts,
    OrderFacts,
    OrderItemFacts,
    ProductCategory,
    ProductTag,
    ShipmentFacts,
    ToolResult,
)
from backend.app.evaluation.models import EvaluationCase
from backend.app.services.after_sales_cases import (
    AfterSalesCaseNotFound,
    AfterSalesCaseValidationError,
)
from backend.app.services.customer_operations import (
    CustomerOperationDependencyError,
    CustomerOperationNotFound,
    CustomerOperationValidationError,
)


_ORDER_PATTERN = re.compile(r"ORD-\d{8}-\d{3}")
_DEFAULT_CUSTOMER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
_SECONDARY_CUSTOMER_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")


def _case_order_id(case: EvaluationCase) -> str:
    if case.fixture == "structured_context":
        return "ORD-20260929-008"
    match = _ORDER_PATTERN.search(case.prompt)
    if match is None:
        for turn in reversed(case.history):
            match = _ORDER_PATTERN.search(turn.content)
            if match is not None:
                break
    return match.group(0) if match else "ORD-20260927-999"


def _build_order(case: EvaluationCase) -> tuple[OrderFacts, uuid.UUID]:
    now = datetime.now(UTC)
    fixture = case.fixture
    order_id = _case_order_id(case)
    category = (
        ProductCategory.FOOD_FRESH
        if fixture.startswith("fresh")
        else ProductCategory.DIGITAL
    )
    product_name = "智利车厘子礼盒" if category == ProductCategory.FOOD_FRESH else "无线降噪耳机"
    product_tags = [ProductTag.FRESH] if category == ProductCategory.FOOD_FRESH else []
    status = "completed"
    payment_status = "paid"
    signed_at: datetime | None = now - timedelta(hours=1)
    shipped_at: datetime | None = now - timedelta(days=1)

    if fixture == "fresh_expired":
        signed_at = now - timedelta(hours=3)
    elif fixture == "digital_within":
        signed_at = now - timedelta(hours=47)
    elif fixture == "digital_expired":
        signed_at = now - timedelta(hours=49)
    elif fixture == "unpaid":
        status = "pending_payment"
        payment_status = "unpaid"
        signed_at = None
        shipped_at = None
    elif fixture == "cancelled":
        status = "cancelled"
        payment_status = "paid"
        signed_at = None
        shipped_at = None
    elif fixture in {"paid_unshipped", "address_valid", "reminder_valid"}:
        status = "paid"
        signed_at = None
        shipped_at = None
    elif fixture == "shipped_unsigned":
        status = "shipped"
        signed_at = None
    elif fixture == "already_refunded":
        status = "refunded"
        payment_status = "refunded"
        signed_at = None
        shipped_at = None
    elif fixture == "unknown_state":
        status = "manual_review"
        payment_status = "review_required"
        signed_at = None
        shipped_at = None

    shipments = []
    if shipped_at is not None:
        shipments = [
            ShipmentFacts(
                shipment_id=f"SHP-{order_id[4:]}-01",
                carrier="顺丰速运",
                tracking_number="SF-EVAL-0001",
                tracking_status="已签收" if signed_at else "运输中",
                shipped_at=shipped_at,
                signed_at=signed_at,
                updated_at=now,
            )
        ]
    owner = _SECONDARY_CUSTOMER_ID if fixture == "unauthorized" else _DEFAULT_CUSTOMER_ID
    items = [
        OrderItemFacts(
            item_id=f"ITEM-{order_id[4:]}-01",
            sku_id="SKU-EVAL-001",
            product_name=product_name,
            product_category=category,
            product_tags=product_tags,
            quantity=1,
            unit_price=Decimal("329.00"),
        )
    ]
    if fixture == "multi_item":
        items.append(
            OrderItemFacts(
                item_id=f"ITEM-{order_id[4:]}-02",
                sku_id="SKU-EVAL-002",
                product_name="智能运动手表",
                product_category=ProductCategory.DIGITAL,
                product_tags=[],
                quantity=1,
                unit_price=Decimal("699.00"),
            )
        )

    order = OrderFacts(
        order_id=order_id,
        status=status,
        payment_status=payment_status,
        amount=Decimal("329.00"),
        currency="CNY",
        created_at=now - timedelta(days=2),
        paid_at=None if payment_status == "unpaid" else now - timedelta(days=2),
        items=items,
        shipments=shipments,
    )
    return order, owner


class EvaluationOrderService:
    def __init__(self, case: EvaluationCase, *, timeout_seconds: float) -> None:
        self.case = case
        self.timeout_seconds = timeout_seconds
        self.order, self.owner = _build_order(case)

    async def get_order(self, order_id: str, customer_id: uuid.UUID) -> ToolResult:
        if self.case.fixture == "order_timeout":
            await asyncio.sleep(self.timeout_seconds * 3)
        if self.case.fixture == "order_unavailable":
            return ToolResult(
                success=False,
                error_code="ORDER_SOURCE_UNAVAILABLE",
                message="订单服务暂时不可用，请稍后重试。",
            )
        if self.case.fixture == "order_malformed":
            return ToolResult(
                success=True,
                message="订单查询成功",
                data={"order_id": order_id, "invalid": True},
            )
        if order_id != self.order.order_id or customer_id != self.owner:
            return ToolResult(
                success=False,
                error_code="ORDER_NOT_FOUND",
                message="未找到该订单，请核对订单号。",
            )
        return ToolResult(
            success=True,
            message="订单查询成功",
            data=self.order.model_dump(mode="json"),
        )

    async def get_logistics(self, order_id: str, customer_id: uuid.UUID) -> ToolResult:
        if self.case.fixture == "logistics_timeout":
            await asyncio.sleep(self.timeout_seconds * 3)
        if self.case.fixture == "logistics_malformed":
            return ToolResult(
                success=True,
                message="物流查询成功",
                data={"order_id": order_id, "shipments": "invalid"},
            )
        order = await self.get_order(order_id, customer_id)
        if not order.success:
            return order
        return ToolResult(
            success=True,
            message="物流信息查询成功",
            data=LogisticsFacts(
                order_id=order_id,
                shipments=self.order.shipments,
            ).model_dump(mode="json"),
        )


class KnowledgeSearchService(Protocol):
    async def search(
        self,
        query: str,
        *,
        top_k: int,
        policy_category: str,
        product_category: str | None,
    ) -> list[dict]: ...


class EvaluationKnowledgeService:
    """Fault-injection wrapper with an optional real knowledge backend."""

    def __init__(
        self,
        case: EvaluationCase,
        *,
        timeout_seconds: float,
        delegate: KnowledgeSearchService | None = None,
    ) -> None:
        self.case = case
        self.timeout_seconds = timeout_seconds
        self.delegate = delegate

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        policy_category: str,
        product_category: str | None,
    ) -> list[dict]:
        if self.case.fixture == "knowledge_timeout":
            await asyncio.sleep(self.timeout_seconds * 3)
        if self.case.fixture == "knowledge_unavailable":
            raise RuntimeError("Knowledge service unavailable")
        if self.delegate is not None:
            return await self.delegate.search(
                query,
                top_k=top_k,
                policy_category=policy_category,
                product_category=product_category,
            )
        fresh = product_category == ProductTag.FRESH.value or "生鲜" in query or "车厘子" in query
        source_filename = "特殊商品退款例外规则.md" if fresh else "物流异常处理办法.md"
        section = "1. 生鲜食品类" if fresh else "3. 运输破损"
        title = "特殊商品退款例外规则" if fresh else "物流异常处理办法"
        content = (
            "生鲜食品签收后发现破损，应在2小时内提供材料申请退款或补发。"
            if fresh
            else "商品运输破损应在签收后48小时内提供材料，可申请退款、换货或补发。"
        )
        chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source_filename}:{section}"))
        return [
            {
                "document_id": str(uuid.uuid5(uuid.NAMESPACE_URL, source_filename)),
                "chunk_id": chunk_id,
                "title": title,
                "section": section,
                "content": content,
                "product_categories": [ProductTag.FRESH.value] if fresh else ["全品类"],
                "score": 1.0,
                "citation": {
                    "source_filename": source_filename,
                    "section": section,
                },
            }
        ][:top_k]


class EvaluationTicketService:
    def __init__(self, case: EvaluationCase) -> None:
        self.case = case

    async def create(self, **kwargs) -> ToolResult:
        if self.case.fixture == "ticket_unavailable":
            return ToolResult(
                success=False,
                error_code="TICKET_SERVICE_UNAVAILABLE",
                message="人工工单服务暂时不可用，请稍后重试。",
            )
        run_id = kwargs["run_id"]
        return ToolResult(
            success=True,
            message="人工工单已创建",
            data={"ticket_id": str(run_id), "status": "open"},
        )


class EvaluationAfterSalesCaseService:
    def __init__(self, case: EvaluationCase) -> None:
        self.case = case

    async def find_existing_case(self, **kwargs):
        if self.case.fixture == "existing_after_sales":
            order_no = kwargs["order_no"]
            order_item_no = kwargs["order_item_no"]
            return SimpleNamespace(
                case_no="AS-EVAL-EXISTING",
                order_no=order_no,
                order_item_no=order_item_no,
                case_type="refund",
                status="SUBMITTED",
                evidence_required=True,
                problem_discovered_at_required=False,
                evidence_deadline_at=None,
                deadline_status="within_deadline",
            )
        return None

    async def stage_decision(self, **kwargs):
        order = kwargs["order"]
        item = kwargs["order_item"]
        deadline = kwargs.get("policy_deadline")
        return SimpleNamespace(
            case_no=f"AS-EVAL-{self.case.id}",
            order_no=order.order_id,
            order_item_no=item.item_id,
            case_type=kwargs["decision"].case_type,
            status="WAITING_MATERIALS",
            evidence_required=kwargs["decision"].reason_code != "PRE_SHIPMENT_REFUND_ALLOWED",
            problem_discovered_at_required=False,
            evidence_deadline_at=getattr(deadline, "deadline_at", None),
            deadline_status=getattr(deadline, "status", None),
        )

    async def confirm_latest_case(self, **kwargs):
        if self.case.fixture == "after_sales_not_ready":
            raise AfterSalesCaseValidationError("没有等待提交的售后申请")
        order_id = _case_order_id(self.case)
        return SimpleNamespace(
            case_no=f"AS-EVAL-{self.case.id}",
            order_no=order_id,
            order_item_no=f"ITEM-{order_id[4:]}-01",
            case_type="refund",
            status="SUBMITTED",
            evidence_required=True,
            problem_discovered_at_required=True,
        )

    async def get_latest_for_order(self, customer_id, order_no):
        if self.case.fixture in {"after_sales_missing", "unauthorized"}:
            raise AfterSalesCaseNotFound("未找到该售后申请")
        return self._status_case(order_no)

    async def get_case(self, customer_id, case_no):
        if self.case.fixture in {"after_sales_missing", "unauthorized"}:
            raise AfterSalesCaseNotFound("未找到该售后申请")
        return self._status_case(_case_order_id(self.case), case_no=case_no)

    def _status_case(self, order_no: str, *, case_no: str = "AS-EVAL-STATUS"):
        status = {
            "after_sales_under_review": "UNDER_REVIEW",
            "after_sales_completed": "COMPLETED",
            "after_sales_failed": "EXECUTION_FAILED",
        }.get(self.case.fixture, "SUBMITTED")
        return SimpleNamespace(
            case_no=case_no,
            order_no=order_no,
            order_item_no=f"ITEM-{order_no[4:]}-01",
            case_type="repair",
            status=status,
            evidence_required=True,
            problem_discovered_at_required=False,
            evidence_deadline_at=None,
            deadline_status="within_deadline",
        )


class EvaluationCustomerOperationService:
    def __init__(self, case: EvaluationCase) -> None:
        self.case = case

    @staticmethod
    def _row(order_no: str, request_type: str, *, status: str) -> SimpleNamespace:
        prefix = {
            "address_change": "ADDR",
            "shipment_reminder": "REM",
            "invoice_application": "INV",
        }[request_type]
        return SimpleNamespace(
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"{prefix}:{order_no}"),
            request_no=f"{prefix}-EVAL-{order_no[-3:]}",
            order_no=order_no,
            request_type=request_type,
            status=status,
            result_payload={"provider": "mock"} if status == "COMPLETED" else {},
        )

    def _guard(self) -> None:
        if self.case.fixture == "unauthorized":
            raise CustomerOperationNotFound("未找到该订单")
        if self.case.fixture == "operation_unavailable":
            raise CustomerOperationDependencyError("外部业务系统暂时不可用")
        if self.case.fixture in {
            "address_invalid_state",
            "reminder_invalid_state",
            "invoice_invalid_state",
        }:
            raise CustomerOperationValidationError("当前订单状态不允许该操作")

    async def stage_form(self, *, order_no: str, request_type: str, **kwargs):
        self._guard()
        return self._row(order_no, request_type, status="DRAFT")

    async def request_shipment_reminder(self, *, order_no: str, **kwargs):
        self._guard()
        return self._row(order_no, "shipment_reminder", status="COMPLETED")

    async def list(self, customer_id, *, order_no: str, request_type: str, **kwargs):
        if self.case.fixture == "unauthorized":
            return []
        self._guard()
        if self.case.fixture == "invoice_missing":
            return []
        return [self._row(order_no, request_type, status="COMPLETED")]


class EvaluationConversationContextService:
    def __init__(self, case: EvaluationCase) -> None:
        self.case = case

    async def get(self, **kwargs) -> ConversationContext:
        if self.case.fixture == "structured_context":
            return ConversationContext(active_order_id="ORD-20260929-008")
        return ConversationContext()

    async def set_active_order(self, **kwargs) -> None:
        return None

    async def clear_active_order(self, **kwargs) -> None:
        return None
