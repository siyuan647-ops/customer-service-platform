from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


RagCaseCategory = Literal[
    "standard",
    "paraphrase",
    "colloquial",
    "product_boundary",
    "multi_policy",
    "exception",
    "no_answer",
    "confusing",
]

PolicyCategory = Literal["refund", "shipping", "after_sale", "general"]


class RelevantRule(BaseModel):
    """A stable judgment that survives re-chunking.

    Chunk ids are deliberately not used because changing chunk_size or overlap
    recreates them. A rule is identified by its source document and section.
    """

    source_filename: str
    section: str
    relevance: int = Field(default=3, ge=1, le=3)

    @property
    def key(self) -> tuple[str, str]:
        return self.source_filename, self.section


class RagEvaluationCase(BaseModel):
    id: str
    category: RagCaseCategory
    query: str = Field(min_length=2)
    policy_category: PolicyCategory = "general"
    product_category: str | None = None
    relevant_rules: list[RelevantRule] = Field(default_factory=list)
    expect_no_answer: bool = False
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_judgments(self) -> "RagEvaluationCase":
        if self.expect_no_answer and self.relevant_rules:
            raise ValueError("no-answer cases cannot contain relevant rules")
        if not self.expect_no_answer and not self.relevant_rules:
            raise ValueError("answerable cases require at least one relevant rule")
        return self


class RagExperimentConfig(BaseModel):
    chunk_size: int = Field(ge=200)
    overlap: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_overlap(self) -> "RagExperimentConfig":
        if self.overlap >= self.chunk_size:
            raise ValueError("overlap must be smaller than chunk_size")
        return self

    @property
    def name(self) -> str:
        return f"chunk-{self.chunk_size}-overlap-{self.overlap}"


class RagMetrics(BaseModel):
    top_k: int
    answerable_cases: int
    no_answer_cases: int
    recall: float
    mrr: float
    ndcg: float
    precision: float
    duplicate_rate: float
    critical_miss_rate: float
    no_answer_accuracy: float
    average_returned: float
    latency_mean_ms: float
    latency_p95_ms: float


class RagConfigResult(BaseModel):
    name: str
    chunk_size: int
    overlap: int
    chunk_count: int
    max_chunk_chars: int
    index_signature: str
    index_build_ms: float
    metrics: dict[str, RagMetrics]


class RagEvaluationReport(BaseModel):
    schema_version: str = "1.0"
    dataset_version: str
    embedding_model: str
    generated_at: str
    case_count: int
    category_counts: dict[str, int]
    top_k_values: list[int]
    configurations: list[RagConfigResult]
    equivalent_index_groups: list[list[str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ThresholdMetrics(BaseModel):
    threshold: float
    split: Literal["calibration", "test", "all"]
    top_k: int
    metrics: RagMetrics
    objective: float


class RerankerThresholdReport(BaseModel):
    schema_version: str = "1.0"
    dataset_version: str
    embedding_model: str
    reranker_model: str
    generated_at: str
    candidate_limit: int
    selected_threshold: float
    selection_rule: str
    calibration_case_count: int
    test_case_count: int
    results: list[ThresholdMetrics]
