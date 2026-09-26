from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


PolicyCategory = Literal["refund", "shipping", "after_sale", "general"]


class ProductCategory(StrEnum):
    FOOD_FRESH = "食品生鲜"
    CLOTHING = "服饰鞋包"
    BEAUTY = "美妆个护"
    DIGITAL = "数码电器"
    HOME_APPLIANCE = "家居家电"
    MATERNAL_CHILD = "母婴用品"
    SPORTS = "运动户外"
    BOOKS = "图书文娱"
    PET = "宠物用品"
    HEALTH = "医药保健"
    VIRTUAL = "虚拟商品"
    OTHER = "其他"


class ProductTag(StrEnum):
    FRESH = "生鲜食品"
    CUSTOMIZED = "定制商品"
    VIRTUAL = "虚拟商品"
    PERSONAL = "贴身用品"


class GetOrderArgs(BaseModel):
    order_id: str = Field(pattern=r"^ORD-\d{8}-\d{3}$")


class GetLogisticsArgs(BaseModel):
    order_id: str = Field(pattern=r"^ORD-\d{8}-\d{3}$")


class SearchPolicyArgs(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    category: PolicyCategory = "general"
    product_category: str | None = Field(default=None, max_length=100)
    top_k: int = Field(default=3, ge=1, le=5)


class CreateHumanTicketArgs(BaseModel):
    reason: Literal["refund_dispute", "damaged_goods", "account_risk", "other"]
    summary: str = Field(min_length=4, max_length=500)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"


class CreateAfterSalesCaseArgs(BaseModel):
    confirmed: Literal[True]


class ToolResult(BaseModel):
    success: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None
    message: str


class ConversationTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class SupervisorPlan(BaseModel):
    intent: Literal[
        "order_query",
        "logistics_query",
        "policy_query",
        "after_sales",
        "after_sales_confirm",
        "human_handoff",
        "general",
    ]
    order_id: str | None = None
    order_item_id: str | None = None
    product_category: ProductCategory | None = None
    product_tags: list[ProductTag] = Field(default_factory=list)
    policy_category: PolicyCategory = "general"
    need_order_data: bool = False
    need_policy_evidence: bool = False
    need_after_sales_decision: bool = False
    missing_information: list[str] = Field(default_factory=list)


class OrderItemFacts(BaseModel):
    item_id: str
    sku_id: str
    product_name: str
    product_category: ProductCategory
    product_tags: list[ProductTag] = Field(default_factory=list)
    quantity: int
    unit_price: Decimal


class ShipmentFacts(BaseModel):
    shipment_id: str
    carrier: str | None = None
    tracking_number: str | None = None
    tracking_status: str
    shipped_at: datetime | None = None
    signed_at: datetime | None = None
    updated_at: datetime


class OrderFacts(BaseModel):
    order_id: str
    status: str
    payment_status: str
    amount: Decimal
    currency: str
    created_at: datetime
    paid_at: datetime | None = None
    items: list[OrderItemFacts] = Field(default_factory=list)
    shipments: list[ShipmentFacts] = Field(default_factory=list)


class LogisticsFacts(BaseModel):
    order_id: str
    shipments: list[ShipmentFacts] = Field(default_factory=list)


class EvidencePrecheckResult(BaseModel):
    media_quality: Literal["clear", "blurry", "unusable"]
    issue_visible: bool
    detected_issues: list[str] = Field(default_factory=list, max_length=8)
    product_visible: bool
    package_visible: bool
    waybill_visible: bool
    extracted_tracking_number: str | None = Field(default=None, max_length=128)
    summary: str = Field(min_length=2, max_length=1000)
    risk_flags: list[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(ge=0, le=1)


class PolicyReference(BaseModel):
    title: str
    section: str
    source_filename: str
    quote: str


class PolicyEvidence(BaseModel):
    document_id: str
    chunk_id: str
    title: str
    section: str
    content: str
    product_categories: list[str]
    source_filename: str
    score: float


class PolicyDeadlineFacts(BaseModel):
    status: Literal[
        "not_applicable",
        "within_deadline",
        "expired",
        "unknown",
        "conflict",
    ]
    window_hours: int | None = None
    signed_at: datetime | None = None
    requested_at: datetime
    deadline_at: datetime | None = None
    remaining_seconds: int | None = None
    policy_references: list[PolicyReference] = Field(default_factory=list)


class AfterSalesResult(BaseModel):
    decision: Literal[
        "eligible",
        "ineligible",
        "need_more_information",
        "policy_conflict",
    ]
    reason_code: str = Field(min_length=2, max_length=64)
    reason: str = Field(min_length=2, max_length=1000)
    required_information: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    policy_references: list[PolicyReference] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"] = "low"
    should_handoff: bool = False


class AfterSalesCaseFacts(BaseModel):
    case_no: str
    order_id: str
    order_item_id: str
    case_type: Literal["refund", "return", "exchange", "reship", "repair"]
    status: str
    evidence_required: bool
    problem_discovered_at_required: bool
    evidence_deadline_at: datetime | None = None
    deadline_status: str | None = None


class WorkflowResult(BaseModel):
    status: Literal[
        "success",
        "need_more_information",
        "not_found",
        "handoff_created",
        "error",
    ]
    plan: SupervisorPlan
    order: OrderFacts | None = None
    order_item: OrderItemFacts | None = None
    logistics: LogisticsFacts | None = None
    evidence: list[PolicyEvidence] = Field(default_factory=list)
    policy_deadline: PolicyDeadlineFacts | None = None
    after_sales: AfterSalesResult | None = None
    after_sales_case: AfterSalesCaseFacts | None = None
    ticket_id: str | None = None
    required_information: list[str] = Field(default_factory=list)
    message: str | None = None
    warnings: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class AgentRequest:
    run_id: uuid.UUID
    conversation_id: uuid.UUID
    customer_id: uuid.UUID
    prompt: str
    history: list[ConversationTurn] = field(default_factory=list)


@dataclass(slots=True)
class AgentOutcome:
    final_output: str
    tool_call_count: int
    trace: list[dict[str, Any]] = field(default_factory=list)
