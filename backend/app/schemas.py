from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    status: str
    created_at: datetime


class MessageAccepted(BaseModel):
    conversation_id: uuid.UUID
    message: MessageRead
    status: str = "accepted"


class ConversationRead(BaseModel):
    id: uuid.UUID
    status: str
    channel: str
    messages: list[MessageRead]


class HealthRead(BaseModel):
    status: str
    service: str
    components: dict[str, str]


class KnowledgeIngestRead(BaseModel):
    document_id: uuid.UUID
    title: str
    source_filename: str
    product_categories: list[str]
    chunk_count: int
    unchanged: bool


class KnowledgeDocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    source_filename: str
    product_categories: list[str]
    content_hash: str
    embedding_model: str
    status: str
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentDetail(KnowledgeDocumentRead):
    raw_text: str
    document_metadata: dict[str, str]


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    top_k: int = Field(default=5, ge=1, le=10)
    policy_category: Literal["refund", "shipping", "after_sale", "general"] = "general"
    product_category: str | None = Field(default=None, max_length=100)


class KnowledgeCitation(BaseModel):
    source_filename: str
    title: str
    section: str
    chunk_index: int


class KnowledgeSearchResult(BaseModel):
    document_id: uuid.UUID
    chunk_id: uuid.UUID
    title: str
    section: str
    content: str
    product_categories: list[str]
    score: float
    vector_score: float
    bm25_score: float
    vector_rank: int | None
    bm25_rank: int | None
    rerank_score: float | None = None
    citation: KnowledgeCitation


class KnowledgeSearchResponse(BaseModel):
    query: str
    results: list[KnowledgeSearchResult]


AfterSalesCaseType = Literal["refund", "return", "exchange", "reship", "repair"]


class AfterSalesCaseCreate(BaseModel):
    order_id: str = Field(pattern=r"^ORD-\d{8}-\d{3}$")
    order_item_id: str = Field(pattern=r"^ITEM-\d{8}-\d{3}-\d{2}$")
    case_type: AfterSalesCaseType
    reason: str = Field(min_length=4, max_length=1000)
    idempotency_key: str = Field(min_length=8, max_length=128)


class CustomerServiceRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    request_no: str
    conversation_id: uuid.UUID | None
    order_id: str = Field(validation_alias="order_no")
    request_type: Literal["address_change", "shipment_reminder", "invoice_application"]
    status: str
    request_payload: dict
    result_payload: dict
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class ShippingAddressUpdate(BaseModel):
    recipient: str = Field(min_length=2, max_length=50)
    phone: str = Field(pattern=r"^1\d{10}$")
    province: str = Field(min_length=2, max_length=50)
    city: str = Field(min_length=2, max_length=50)
    district: str = Field(min_length=2, max_length=50)
    detail: str = Field(min_length=4, max_length=200)


class InvoiceApplicationUpdate(BaseModel):
    invoice_type: Literal["electronic_general", "vat_special"]
    title_type: Literal["personal", "company"]
    title: str = Field(min_length=2, max_length=100)
    tax_number: str | None = Field(default=None, min_length=8, max_length=30)
    email: str = Field(
        pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$",
        max_length=254,
    )


class AfterSalesEvidenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    status: str
    analysis_status: str
    created_at: datetime


class AdminAfterSalesEvidenceRead(AfterSalesEvidenceRead):
    analysis_result: dict
    analysis_model: str | None
    analysis_prompt_version: str | None
    analysis_error: str | None
    analyzed_at: datetime | None


class AfterSalesCaseMaterialUpdate(BaseModel):
    case_type: AfterSalesCaseType | None = None
    problem_type: str | None = Field(default=None, min_length=2, max_length=100)
    problem_discovered_at: datetime | None = None
    problem_description: str | None = Field(default=None, min_length=2, max_length=1000)


class AfterSalesActionLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    from_status: str | None
    to_status: str
    action: str
    actor_type: str
    actor_id: str | None
    action_metadata: dict
    created_at: datetime


class AfterSalesReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    reviewer_id: str
    decision: str
    action: str | None
    refund_amount: Decimal | None
    reason: str
    created_at: datetime


class AfterSalesOperationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    operation_type: str
    provider: str
    status: str
    external_request_id: str | None
    request_payload: dict
    response_payload: dict
    attempt_count: int
    last_error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class AfterSalesCaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    id: uuid.UUID
    case_no: str
    customer_id: uuid.UUID
    order_id: str = Field(validation_alias="order_no")
    order_item_id: str = Field(validation_alias="order_item_no")
    case_type: AfterSalesCaseType | None
    reason: str
    problem_type: str | None
    problem_description: str | None
    problem_discovered_at: datetime | None
    status: str
    decision: str | None
    reason_code: str | None
    decision_reason: str | None
    risk_level: str
    should_handoff: bool
    evidence_required: bool
    problem_discovered_at_required: bool
    evidence_deadline_at: datetime | None
    deadline_window_hours: int | None
    deadline_status: str | None
    resolution_action: str | None
    refund_amount: Decimal | None
    failure_reason: str | None
    required_information: list[str]
    recommended_actions: list[str]
    policy_references: list[dict]
    ticket_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None
    reviewed_at: datetime | None
    approved_at: datetime | None
    rejected_at: datetime | None
    execution_started_at: datetime | None
    completed_at: datetime | None
    evidence: list[AfterSalesEvidenceRead] = Field(default_factory=list)
    action_logs: list[AfterSalesActionLogRead] = Field(default_factory=list)
    reviews: list[AfterSalesReviewRead] = Field(default_factory=list)
    operations: list[AfterSalesOperationRead] = Field(default_factory=list)


class AfterSalesApprovalRequest(BaseModel):
    action: Literal[
        "cancel_and_refund",
        "refund_only",
        "return_and_refund",
        "exchange",
        "reship",
        "repair",
    ]
    refund_amount: Decimal | None = Field(
        default=None, gt=0, max_digits=12, decimal_places=2
    )
    reason: str = Field(min_length=2, max_length=1000)


class AfterSalesRejectionRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)


class AdminAfterSalesCaseRead(AfterSalesCaseRead):
    evidence: list[AdminAfterSalesEvidenceRead] = Field(default_factory=list)
    max_refund_amount: Decimal
    currency: str
