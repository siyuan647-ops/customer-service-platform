from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

from backend.app.config import Settings
from backend.app.knowledge.embeddings import HashEmbeddingProvider
from backend.app.rag_evaluation.dataset import (
    EXPECTED_CASE_COUNT,
    EXPECTED_CATEGORY_COUNTS,
    load_rag_evaluation_cases,
)
from backend.app.rag_evaluation.reporting import write_dataset, write_report
from backend.app.rag_evaluation.runner import (
    DEFAULT_EXPERIMENT_CONFIGS,
    DEFAULT_TOP_K_VALUES,
    RagEvaluationRunner,
)


def test_rag_dataset_has_expected_coverage() -> None:
    cases = load_rag_evaluation_cases()

    assert len(cases) == EXPECTED_CASE_COUNT
    assert Counter(case.category for case in cases) == EXPECTED_CATEGORY_COUNTS
    assert len({case.id for case in cases}) == len(cases)
    tags = {tag for case in cases for tag in case.tags}
    assert {"退款", "换货", "维修", "补发", "物流", "发票"} <= tags


def test_rag_parameter_matrix_runs_and_reports_equivalent_current_chunks(
    tmp_path: Path,
) -> None:
    settings = Settings(embedding_mode="hash")
    embeddings = HashEmbeddingProvider(settings.embedding_dimensions)
    report = asyncio.run(
        RagEvaluationRunner(
            settings=settings,
            embeddings=embeddings,
            knowledge_dir=Path("knowledge_docs"),
        ).run(load_rag_evaluation_cases())
    )

    assert len(report.configurations) == len(DEFAULT_EXPERIMENT_CONFIGS) == 9
    assert report.top_k_values == list(DEFAULT_TOP_K_VALUES)
    assert len({item.index_signature for item in report.configurations}) == 1
    assert all(
        set(item.metrics) == {"3", "5", "8"}
        for item in report.configurations
    )
    assert all(
        0 <= metric.recall <= 1
        and 0 <= metric.mrr <= 1
        and 0 <= metric.ndcg <= 1
        and 0 <= metric.precision <= 1
        for item in report.configurations
        for metric in item.metrics.values()
    )
    assert any("same chunks" in warning for warning in report.warnings)

    json_path, markdown_path = write_report(report, tmp_path)
    dataset_path = write_dataset(load_rag_evaluation_cases(), tmp_path)
    assert json_path.is_file()
    assert markdown_path.is_file()
    assert dataset_path.is_file()
    assert "Recall" in markdown_path.read_text(encoding="utf-8")
