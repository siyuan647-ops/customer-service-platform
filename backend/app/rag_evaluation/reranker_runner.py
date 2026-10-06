from __future__ import annotations

import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from backend.app.config import Settings
from backend.app.knowledge.embeddings import EmbeddingProvider
from backend.app.knowledge.reranker import Reranker
from backend.app.rag_evaluation.dataset import DATASET_VERSION
from backend.app.rag_evaluation.models import (
    RagEvaluationCase,
    RagExperimentConfig,
    RerankerThresholdReport,
    ThresholdMetrics,
)
from backend.app.rag_evaluation.runner import (
    _OfflineHybridIndex,
    _RankedChunk,
    RagEvaluationRunner,
)


class RerankerThresholdRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        embeddings: EmbeddingProvider,
        reranker: Reranker,
        knowledge_dir: Path,
        thresholds: list[float] | None = None,
    ) -> None:
        self.settings = settings
        self.embeddings = embeddings
        self.reranker = reranker
        self.knowledge_dir = knowledge_dir
        self.thresholds = thresholds or [value / 100 for value in range(0, 101)]

    async def run(self, cases: list[RagEvaluationCase]) -> RerankerThresholdReport:
        base = RagEvaluationRunner(
            settings=self.settings,
            embeddings=self.embeddings,
            knowledge_dir=self.knowledge_dir,
            configurations=(RagExperimentConfig(chunk_size=900, overlap=120),),
            top_k_values=(3, 5, 8),
        )
        documents = base._load_documents()
        query_vectors, embedding_ms = await base._embed_queries(cases)
        chunks = await base._build_chunks(
            documents,
            RagExperimentConfig(chunk_size=900, overlap=120),
        )
        index = _OfflineHybridIndex(
            chunks=chunks,
            settings=self.settings,
            candidate_limit=max(
                self.settings.knowledge_retrieval_candidates,
                self.settings.reranker_candidate_limit * 2,
            ),
        )

        rrf_rankings: dict[str, list[_RankedChunk]] = {
            case.id: index.search(
                case,
                query_vectors[case.id],
                limit=self.settings.reranker_candidate_limit,
            )
            for case in cases
        }
        reranker_ms: dict[str, float] = {}
        for case in cases:
            rows = rrf_rankings[case.id]
            pairs = [
                (
                    case.query,
                    "\n".join(
                        (
                            row.chunk.title,
                            row.chunk.section,
                            row.chunk.content,
                        ),
                    ),
                )
                for row in rows
            ]
            started = time.perf_counter()
            scores = await self.reranker.score_pairs(pairs)
            reranker_ms[case.id] = (time.perf_counter() - started) * 1000
            for row, score in zip(rows, scores, strict=True):
                row.rerank_score = score

        calibration, test = _stratified_split(cases)
        results: list[ThresholdMetrics] = []
        for threshold in self.thresholds:
            filtered = _filter_rankings(rrf_rankings, threshold)
            for split_name, split_cases in (
                ("calibration", calibration),
                ("test", test),
                ("all", cases),
            ):
                latencies = {
                    case.id: embedding_ms[case.id] + reranker_ms[case.id]
                    for case in split_cases
                }
                for top_k in (3, 5, 8):
                    metrics = RagEvaluationRunner._score(
                        split_cases,
                        filtered,
                        latencies,
                        top_k=top_k,
                    )
                    results.append(
                        ThresholdMetrics(
                            threshold=threshold,
                            split=split_name,
                            top_k=top_k,
                            metrics=metrics,
                            objective=_objective(metrics),
                        )
                    )

        selected = _select_threshold(results)
        return RerankerThresholdReport(
            dataset_version=DATASET_VERSION,
            embedding_model=self.embeddings.name,
            reranker_model=self.reranker.name,
            generated_at=datetime.now(UTC).isoformat(),
            candidate_limit=self.settings.reranker_candidate_limit,
            selected_threshold=selected,
            selection_rule=(
                "On the calibration split, require no-answer accuracy >= 90%, "
                "Recall@5 >= 90%, and critical miss <= 10%; then maximize the "
                "weighted retrieval objective. If infeasible, maximize the objective."
            ),
            calibration_case_count=len(calibration),
            test_case_count=len(test),
            results=results,
        )


def _filter_rankings(
    rankings: dict[str, list[_RankedChunk]], threshold: float
) -> dict[str, list[_RankedChunk]]:
    return {
        case_id: sorted(
            (
                row
                for row in rows
                if row.rerank_score is not None and row.rerank_score >= threshold
            ),
            key=lambda row: (row.rerank_score or 0.0, row.rrf_score),
            reverse=True,
        )
        for case_id, rows in rankings.items()
    }


def _stratified_split(
    cases: list[RagEvaluationCase],
) -> tuple[list[RagEvaluationCase], list[RagEvaluationCase]]:
    grouped: dict[str, list[RagEvaluationCase]] = defaultdict(list)
    for case in cases:
        grouped[case.category].append(case)

    calibration: list[RagEvaluationCase] = []
    test: list[RagEvaluationCase] = []
    for category_cases in grouped.values():
        ordered = sorted(category_cases, key=lambda case: case.id)
        calibration_count = max(1, round(len(ordered) * 0.7))
        calibration.extend(ordered[:calibration_count])
        test.extend(ordered[calibration_count:])
    return calibration, test


def _objective(metrics) -> float:
    return round(
        0.30 * metrics.recall
        + 0.20 * metrics.mrr
        + 0.20 * metrics.ndcg
        + 0.15 * metrics.precision
        + 0.15 * metrics.no_answer_accuracy
        - 0.25 * metrics.critical_miss_rate,
        6,
    )


def _select_threshold(results: list[ThresholdMetrics]) -> float:
    calibration_top5 = [
        row for row in results if row.split == "calibration" and row.top_k == 5
    ]
    feasible = [
        row
        for row in calibration_top5
        if row.metrics.no_answer_accuracy >= 0.90
        and row.metrics.recall >= 0.90
        and row.metrics.critical_miss_rate <= 0.10
    ]
    pool = feasible or calibration_top5
    best = max(
        pool,
        key=lambda row: (
            row.objective,
            row.metrics.recall,
            row.metrics.precision,
            row.threshold,
        ),
    )
    return best.threshold
