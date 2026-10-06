from __future__ import annotations

import hashlib
import math
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from backend.app.config import Settings
from backend.app.knowledge.bm25 import bm25_scores
from backend.app.knowledge.embeddings import EmbeddingProvider
from backend.app.knowledge.parser import parse_document, split_document
from backend.app.rag_evaluation.dataset import DATASET_VERSION
from backend.app.rag_evaluation.models import (
    RagConfigResult,
    RagEvaluationCase,
    RagEvaluationReport,
    RagExperimentConfig,
    RagMetrics,
    RelevantRule,
)


DEFAULT_EXPERIMENT_CONFIGS = (
    RagExperimentConfig(chunk_size=600, overlap=60),
    RagExperimentConfig(chunk_size=600, overlap=90),
    RagExperimentConfig(chunk_size=600, overlap=120),
    RagExperimentConfig(chunk_size=900, overlap=90),
    RagExperimentConfig(chunk_size=900, overlap=120),
    RagExperimentConfig(chunk_size=900, overlap=180),
    RagExperimentConfig(chunk_size=1200, overlap=120),
    RagExperimentConfig(chunk_size=1200, overlap=180),
    RagExperimentConfig(chunk_size=1200, overlap=240),
)
DEFAULT_TOP_K_VALUES = (3, 5, 8)

_INTENT_QUERY_TERM = {
    "refund": "退款",
    "shipping": "物流",
    "after_sale": "售后",
}


@dataclass(slots=True)
class _IndexChunk:
    source_filename: str
    title: str
    product_categories: list[str]
    section: str
    content: str
    embedding: list[float]

    @property
    def stable_key(self) -> tuple[str, str]:
        return self.source_filename, _base_section(self.section)


@dataclass(slots=True)
class _RankedChunk:
    chunk: _IndexChunk
    vector_rank: int | None
    bm25_rank: int | None
    rrf_score: float
    rerank_score: float | None = None


class _OfflineHybridIndex:
    """Controlled in-process replica of the production hybrid ranking logic.

    PostgreSQL I/O and HNSW approximation are intentionally excluded so each
    chunking configuration is measured under the same retrieval conditions.
    """

    def __init__(
        self,
        *,
        chunks: list[_IndexChunk],
        settings: Settings,
        candidate_limit: int,
    ) -> None:
        self.chunks = chunks
        self.settings = settings
        self.candidate_limit = candidate_limit

    def search(
        self,
        case: RagEvaluationCase,
        query_vector: list[float],
        *,
        limit: int,
    ) -> list[_RankedChunk]:
        candidates = [
            chunk
            for chunk in self.chunks
            if not case.product_category
            or case.product_category in chunk.product_categories
            or "全品类" in chunk.product_categories
        ]

        vector_rows = sorted(
            ((_cosine(query_vector, chunk.embedding), chunk) for chunk in candidates),
            key=lambda item: item[0],
            reverse=True,
        )[: self.candidate_limit]

        intent_term = _INTENT_QUERY_TERM.get(case.policy_category, "")
        bm25_query = f"{case.query} {intent_term}" if intent_term else case.query
        searchable = [
            "\n".join(
                (
                    chunk.title,
                    " ".join(chunk.product_categories),
                    chunk.section,
                    chunk.content,
                )
            )
            for chunk in candidates
        ]
        scores = bm25_scores(
            bm25_query,
            searchable,
            k1=self.settings.knowledge_bm25_k1,
            b=self.settings.knowledge_bm25_b,
        )
        bm25_rows = sorted(
            (
                (score, chunk)
                for score, chunk in zip(scores, candidates, strict=True)
                if score > 0
            ),
            key=lambda item: item[0],
            reverse=True,
        )[: self.candidate_limit]

        combined: dict[int, _RankedChunk] = {}
        for rank, (_, chunk) in enumerate(vector_rows, start=1):
            combined[id(chunk)] = _RankedChunk(
                chunk=chunk,
                vector_rank=rank,
                bm25_rank=None,
                rrf_score=1.0 / (self.settings.knowledge_rrf_k + rank),
            )
        for rank, (_, chunk) in enumerate(bm25_rows, start=1):
            row = combined.get(id(chunk))
            if row is None:
                row = _RankedChunk(
                    chunk=chunk,
                    vector_rank=None,
                    bm25_rank=None,
                    rrf_score=0.0,
                )
                combined[id(chunk)] = row
            row.bm25_rank = rank
            row.rrf_score += 1.0 / (self.settings.knowledge_rrf_k + rank)

        return sorted(
            combined.values(),
            key=lambda row: (
                row.rrf_score,
                row.vector_rank is not None and row.bm25_rank is not None,
                -(row.vector_rank or self.candidate_limit + 1),
                -(row.bm25_rank or self.candidate_limit + 1),
            ),
            reverse=True,
        )[:limit]


class RagEvaluationRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        embeddings: EmbeddingProvider,
        knowledge_dir: Path,
        configurations: Iterable[RagExperimentConfig] = DEFAULT_EXPERIMENT_CONFIGS,
        top_k_values: Iterable[int] = DEFAULT_TOP_K_VALUES,
    ) -> None:
        self.settings = settings
        self.embeddings = embeddings
        self.knowledge_dir = knowledge_dir
        self.configurations = tuple(configurations)
        self.top_k_values = tuple(sorted(set(top_k_values)))
        if not self.configurations:
            raise ValueError("at least one experiment configuration is required")
        if not self.top_k_values or min(self.top_k_values) < 1:
            raise ValueError("top_k values must be positive")

    async def run(self, cases: list[RagEvaluationCase]) -> RagEvaluationReport:
        if not cases:
            raise ValueError("at least one RAG evaluation case is required")
        documents = self._load_documents()
        query_vectors, query_embedding_ms = await self._embed_queries(cases)
        max_top_k = max(self.top_k_values)
        candidate_limit = max(
            max_top_k * 4,
            self.settings.knowledge_retrieval_candidates,
        )

        config_results: list[RagConfigResult] = []
        for config in self.configurations:
            build_started = time.perf_counter()
            chunks = await self._build_chunks(documents, config)
            index_build_ms = (time.perf_counter() - build_started) * 1000
            index = _OfflineHybridIndex(
                chunks=chunks,
                settings=self.settings,
                candidate_limit=candidate_limit,
            )
            rankings: dict[str, list[_RankedChunk]] = {}
            retrieval_ms: dict[str, float] = {}
            for case in cases:
                started = time.perf_counter()
                rankings[case.id] = index.search(
                    case,
                    query_vectors[case.id],
                    limit=max_top_k,
                )
                scoring_ms = (time.perf_counter() - started) * 1000
                retrieval_ms[case.id] = query_embedding_ms[case.id] + scoring_ms

            metrics = {
                str(top_k): self._score(
                    cases,
                    rankings,
                    retrieval_ms,
                    top_k=top_k,
                )
                for top_k in self.top_k_values
            }
            config_results.append(
                RagConfigResult(
                    name=config.name,
                    chunk_size=config.chunk_size,
                    overlap=config.overlap,
                    chunk_count=len(chunks),
                    max_chunk_chars=max(len(chunk.content) for chunk in chunks),
                    index_signature=_index_signature(chunks),
                    index_build_ms=round(index_build_ms, 3),
                    metrics=metrics,
                )
            )

        equivalent_groups = _equivalent_groups(config_results)
        warnings: list[str] = []
        if len(equivalent_groups) == 1 and len(equivalent_groups[0]) == len(config_results):
            warnings.append(
                "All chunk/overlap configurations produced the same chunks. "
                "The current corpus is section-first and every section is below the "
                "smallest chunk limit, so this corpus cannot distinguish chunking settings."
            )
        elif equivalent_groups:
            warnings.append(
                "Some chunk/overlap configurations produced equivalent indexes; compare "
                "only configurations with different index signatures."
            )
        if any(
            result.metrics[str(top_k)].no_answer_accuracy == 0
            for result in config_results
            for top_k in self.top_k_values
        ):
            warnings.append(
                "No-answer accuracy is zero for at least one configuration. The current "
                "retriever has no minimum relevance threshold and always returns candidates."
            )

        return RagEvaluationReport(
            dataset_version=DATASET_VERSION,
            embedding_model=self.embeddings.name,
            generated_at=datetime.now(UTC).isoformat(),
            case_count=len(cases),
            category_counts=dict(Counter(case.category for case in cases)),
            top_k_values=list(self.top_k_values),
            configurations=config_results,
            equivalent_index_groups=equivalent_groups,
            warnings=warnings,
        )

    def _load_documents(self) -> list[tuple[str, object]]:
        paths = sorted(
            path
            for path in self.knowledge_dir.iterdir()
            if path.is_file() and path.suffix.casefold() in {".md", ".markdown", ".txt"}
        )
        if not paths:
            raise RuntimeError(f"No policy documents found in {self.knowledge_dir}")
        return [
            (path.name, parse_document(path.read_text(encoding="utf-8-sig")))
            for path in paths
        ]

    async def _embed_queries(
        self, cases: list[RagEvaluationCase]
    ) -> tuple[dict[str, list[float]], dict[str, float]]:
        vectors: dict[str, list[float]] = {}
        latencies: dict[str, float] = {}
        await self.embeddings.embed(["知识库检索模型预热"], is_query=True)
        for case in cases:
            started = time.perf_counter()
            vector = (await self.embeddings.embed([case.query], is_query=True))[0]
            latencies[case.id] = (time.perf_counter() - started) * 1000
            vectors[case.id] = vector
        return vectors, latencies

    async def _build_chunks(
        self,
        documents: list[tuple[str, object]],
        config: RagExperimentConfig,
    ) -> list[_IndexChunk]:
        pending: list[tuple[str, object, object]] = []
        inputs: list[str] = []
        for filename, document in documents:
            for chunk in split_document(
                document,
                max_chars=config.chunk_size,
                overlap_chars=config.overlap,
            ):
                pending.append((filename, document, chunk))
                inputs.append(
                    f"政策：{document.title}\n章节：{chunk.section}\n{chunk.content}"
                )

        vectors: list[list[float]] = []
        batch_size = self.settings.embedding_batch_size
        for start in range(0, len(inputs), batch_size):
            vectors.extend(
                await self.embeddings.embed(
                    inputs[start : start + batch_size],
                    is_query=False,
                )
            )
        return [
            _IndexChunk(
                source_filename=filename,
                title=document.title,
                product_categories=list(document.product_categories),
                section=chunk.section,
                content=chunk.content,
                embedding=vector,
            )
            for (filename, document, chunk), vector in zip(pending, vectors, strict=True)
        ]

    @staticmethod
    def _score(
        cases: list[RagEvaluationCase],
        rankings: dict[str, list[_RankedChunk]],
        retrieval_ms: dict[str, float],
        *,
        top_k: int,
    ) -> RagMetrics:
        recalls: list[float] = []
        reciprocal_ranks: list[float] = []
        ndcgs: list[float] = []
        precisions: list[float] = []
        duplicate_rates: list[float] = []
        returned_counts: list[int] = []
        critical_misses = 0
        no_answer_hits = 0
        answerable = 0
        no_answer = 0

        for case in cases:
            rows = rankings[case.id][:top_k]
            returned_counts.append(len(rows))
            duplicate_rates.append(_duplicate_rate(rows))
            if case.expect_no_answer:
                no_answer += 1
                if not rows:
                    no_answer_hits += 1
                continue

            answerable += 1
            judgments = {rule.key: rule for rule in case.relevant_rules}
            seen: set[tuple[str, str]] = set()
            gains: list[int] = []
            first_relevant_rank: int | None = None
            for rank, row in enumerate(rows, start=1):
                rule = _matching_rule(row.chunk, case.relevant_rules)
                if rule is None or rule.key in seen:
                    gains.append(0)
                    continue
                seen.add(rule.key)
                gains.append(rule.relevance)
                if first_relevant_rank is None:
                    first_relevant_rank = rank

            recalls.append(len(seen) / len(judgments))
            reciprocal_ranks.append(
                1.0 / first_relevant_rank if first_relevant_rank is not None else 0.0
            )
            precisions.append(len(seen) / len(rows) if rows else 0.0)
            ndcgs.append(_ndcg(gains, case.relevant_rules, top_k))
            critical_rules = {rule.key for rule in case.relevant_rules if rule.relevance == 3}
            if critical_rules and not (critical_rules & seen):
                critical_misses += 1

        latency_values = list(retrieval_ms.values())
        return RagMetrics(
            top_k=top_k,
            answerable_cases=answerable,
            no_answer_cases=no_answer,
            recall=round(statistics.fmean(recalls), 6),
            mrr=round(statistics.fmean(reciprocal_ranks), 6),
            ndcg=round(statistics.fmean(ndcgs), 6),
            precision=round(statistics.fmean(precisions), 6),
            duplicate_rate=round(statistics.fmean(duplicate_rates), 6),
            critical_miss_rate=round(critical_misses / answerable, 6),
            no_answer_accuracy=round(no_answer_hits / no_answer, 6),
            average_returned=round(statistics.fmean(returned_counts), 6),
            latency_mean_ms=round(statistics.fmean(latency_values), 3),
            latency_p95_ms=round(_percentile(latency_values, 0.95), 3),
        )


def _matching_rule(
    chunk: _IndexChunk, rules: list[RelevantRule]
) -> RelevantRule | None:
    base_section = _base_section(chunk.section)
    matches = [
        rule
        for rule in rules
        if rule.source_filename == chunk.source_filename
        and base_section == rule.section
    ]
    return max(matches, key=lambda rule: rule.relevance) if matches else None


def _base_section(section: str) -> str:
    for separator in ("（", "("):
        if separator in section:
            prefix, suffix = section.rsplit(separator, 1)
            if suffix.rstrip("）)").isdigit():
                return prefix
    return section


def _duplicate_rate(rows: list[_RankedChunk]) -> float:
    if not rows:
        return 0.0
    unique = {row.chunk.stable_key for row in rows}
    return (len(rows) - len(unique)) / len(rows)


def _ndcg(gains: list[int], rules: list[RelevantRule], top_k: int) -> float:
    dcg = sum(
        (2**gain - 1) / math.log2(rank + 1)
        for rank, gain in enumerate(gains, start=1)
        if gain > 0
    )
    ideal = sorted((rule.relevance for rule in rules), reverse=True)[:top_k]
    idcg = sum(
        (2**gain - 1) / math.log2(rank + 1)
        for rank, gain in enumerate(ideal, start=1)
    )
    return dcg / idcg if idcg else 0.0


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left)) or 1.0
    right_norm = math.sqrt(sum(value * value for value in right)) or 1.0
    return numerator / (left_norm * right_norm)


def _index_signature(chunks: list[_IndexChunk]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk.source_filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(chunk.section.encode("utf-8"))
        digest.update(b"\0")
        digest.update(chunk.content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _equivalent_groups(results: list[RagConfigResult]) -> list[list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for result in results:
        grouped[result.index_signature].append(result.name)
    return [names for names in grouped.values() if len(names) > 1]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]
