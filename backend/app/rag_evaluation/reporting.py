from __future__ import annotations

import json
from pathlib import Path

from backend.app.rag_evaluation.models import (
    RagEvaluationCase,
    RagEvaluationReport,
    RerankerThresholdReport,
)


def report_markdown(report: RagEvaluationReport) -> str:
    lines = [
        "# Policy RAG parameter evaluation",
        "",
        f"- Dataset: `{report.dataset_version}`",
        f"- Embedding: `{report.embedding_model}`",
        f"- Cases: {report.case_count}",
        f"- Top K: {', '.join(str(value) for value in report.top_k_values)}",
        "",
        "## Results",
        "",
        "| Chunk | Overlap | K | Chunks | Recall | MRR | nDCG | Precision | Duplicate | Critical miss | No-answer accuracy | Avg returned | Mean ms | P95 ms |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for config in report.configurations:
        for top_k in report.top_k_values:
            metric = config.metrics[str(top_k)]
            lines.append(
                f"| {config.chunk_size} | {config.overlap} | {top_k} | "
                f"{config.chunk_count} | {metric.recall:.2%} | {metric.mrr:.2%} | "
                f"{metric.ndcg:.2%} | {metric.precision:.2%} | "
                f"{metric.duplicate_rate:.2%} | {metric.critical_miss_rate:.2%} | "
                f"{metric.no_answer_accuracy:.2%} | {metric.average_returned:.2f} | "
                f"{metric.latency_mean_ms:.3f} | "
                f"{metric.latency_p95_ms:.3f} |"
            )

    lines.extend(["", "## Index equivalence", ""])
    if report.equivalent_index_groups:
        for group in report.equivalent_index_groups:
            lines.append("- " + ", ".join(f"`{name}`" for name in group))
    else:
        lines.append("No configurations produced identical chunk indexes.")

    lines.extend(["", "## Warnings", ""])
    if report.warnings:
        lines.extend(f"- {warning}" for warning in report.warnings)
    else:
        lines.append("No warnings.")
    lines.extend(
        [
            "",
            "## Interpretation notes",
            "",
            "- Recall, MRR, nDCG and Precision are calculated only on answerable cases.",
            "- Precision uses the number of actually returned chunks as its denominator and is deduplicated by policy rule.",
            "- Duplicate rate counts repeated source-document/section pairs in the returned context.",
            "- Latency includes a measured query-embedding time plus in-process hybrid ranking; it excludes PostgreSQL network I/O and HNSW approximation.",
            "- No-answer accuracy requires an empty result. A retriever without a relevance threshold is expected to fail this metric.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def write_report(
    report: RagEvaluationReport, output_dir: Path
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "latest.json"
    markdown_path = output_dir / "latest.md"
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(report_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def write_dataset(cases: list[RagEvaluationCase], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = output_dir / "dataset.json"
    dataset_path.write_text(
        json.dumps(
            [case.model_dump(mode="json") for case in cases],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return dataset_path


def threshold_report_markdown(report: RerankerThresholdReport) -> str:
    lines = [
        "# Reranker threshold evaluation",
        "",
        f"- Dataset: `{report.dataset_version}`",
        f"- Embedding: `{report.embedding_model}`",
        f"- Reranker: `{report.reranker_model}`",
        f"- Candidate limit: {report.candidate_limit}",
        f"- Calibration / test cases: {report.calibration_case_count} / {report.test_case_count}",
        f"- Selected threshold: **{report.selected_threshold:.4f}**",
        f"- Selection: {report.selection_rule}",
        "",
        "## Top-5 threshold sweep",
        "",
        "| Split | Threshold | Recall | MRR | nDCG | Precision | Critical miss | No-answer accuracy | Avg returned | Mean ms | P95 ms | Objective |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    selected = report.selected_threshold
    for row in report.results:
        if row.top_k != 5:
            continue
        if row.split == "calibration" or abs(row.threshold - selected) < 1e-9:
            metric = row.metrics
            lines.append(
                f"| {row.split} | {row.threshold:.4f} | {metric.recall:.2%} | "
                f"{metric.mrr:.2%} | {metric.ndcg:.2%} | {metric.precision:.2%} | "
                f"{metric.critical_miss_rate:.2%} | {metric.no_answer_accuracy:.2%} | "
                f"{metric.average_returned:.2f} | {metric.latency_mean_ms:.3f} | "
                f"{metric.latency_p95_ms:.3f} | {row.objective:.4f} |"
            )
    lines.extend(
        [
            "",
            "Latency includes query embedding and production-shaped reranking of the RRF candidate set; it excludes PostgreSQL network I/O.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def write_threshold_report(
    report: RerankerThresholdReport, output_dir: Path
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "reranker-threshold-latest.json"
    markdown_path = output_dir / "reranker-threshold-latest.md"
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(threshold_report_markdown(report), encoding="utf-8")
    return json_path, markdown_path
