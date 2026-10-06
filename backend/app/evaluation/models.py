from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.app.agents.contracts import ConversationTurn


EvaluationCategory = Literal[
    "normal_order",
    "missing_information",
    "policy_boundary",
    "permission_attack",
    "prompt_injection",
    "tool_failure",
]


class ExpectedBehavior(BaseModel):
    intent: str | None = None
    tools: list[str] = Field(default_factory=list)
    tool_arguments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    workflow_status: str | None = None
    workflow_facts: dict[str, Any] = Field(default_factory=dict)
    request_rejected: bool = False
    expect_runtime_error: bool = False
    require_policy_reference: bool = False
    must_handoff: bool | None = False
    allow_refund_commitment: bool = False
    forbidden_output_terms: list[str] = Field(default_factory=list)
    max_total_tokens: int = 8_192
    max_mock_latency_ms: float = 2_000
    max_live_latency_ms: float = 60_000


class EvaluationCase(BaseModel):
    id: str
    category: EvaluationCategory
    prompt: str = Field(min_length=1)
    history: list[ConversationTurn] = Field(default_factory=list)
    customer_id: str = "00000000-0000-4000-8000-000000000001"
    fixture: str = "default"
    expected: ExpectedBehavior
    tags: list[str] = Field(default_factory=list)


class ToolCallObservation(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class EvaluationObservation(BaseModel):
    case_id: str
    response: str
    rejected: bool = False
    intent: str | None = None
    tools: list[ToolCallObservation] = Field(default_factory=list)
    workflow_result: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    error_type: str | None = None
    latency_ms: float
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost: float = 0


class CheckResult(BaseModel):
    name: str
    passed: bool
    applicable: bool = True
    detail: str = ""


class CaseEvaluation(BaseModel):
    case: EvaluationCase
    observation: EvaluationObservation
    checks: list[CheckResult]
    passed: bool


class MetricSummary(BaseModel):
    passed: int
    total: int
    rate: float


class EvaluationReport(BaseModel):
    schema_version: str = "1.0"
    dataset_version: str
    mode: Literal["mock", "live"]
    model: str
    system_fingerprint: str
    generated_at: str
    duration_ms: float
    total_cases: int
    passed_cases: int
    pass_rate: float
    category_counts: dict[str, int]
    metrics: dict[str, MetricSummary]
    latency_p50_ms: float
    latency_p95_ms: float
    latency_max_ms: float
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost: float
    gate_passed: bool
    gate_failures: list[str] = Field(default_factory=list)
    results: list[CaseEvaluation]


class GateProfile(BaseModel):
    minimum_case_pass_rate: float = Field(ge=0, le=1)
    minimum_metrics: dict[str, float] = Field(default_factory=dict)
    maximum_latency_p95_ms: float | None = Field(default=None, gt=0)
    maximum_estimated_cost: float | None = Field(default=None, ge=0)


class QualityGateConfig(BaseModel):
    schema_version: str = "1.0"
    dataset_version: str
    mock: GateProfile
    live: GateProfile
