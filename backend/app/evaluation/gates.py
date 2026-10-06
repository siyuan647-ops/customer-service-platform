from __future__ import annotations

from pathlib import Path

from backend.app.evaluation.models import EvaluationReport, QualityGateConfig


def load_quality_gate(path: Path) -> QualityGateConfig:
    return QualityGateConfig.model_validate_json(path.read_text(encoding="utf-8"))


def apply_quality_gate(
    report: EvaluationReport,
    config: QualityGateConfig,
    *,
    require_all_metrics: bool = True,
) -> EvaluationReport:
    failures: list[str] = []
    if report.dataset_version != config.dataset_version:
        failures.append(
            "dataset_version: "
            f"expected={config.dataset_version}, actual={report.dataset_version}"
        )

    profile = config.mock if report.mode == "mock" else config.live
    if report.pass_rate < profile.minimum_case_pass_rate:
        failures.append(
            "case_pass_rate: "
            f"required>={profile.minimum_case_pass_rate:.2%}, actual={report.pass_rate:.2%}"
        )

    for metric_name, minimum_rate in profile.minimum_metrics.items():
        metric = report.metrics.get(metric_name)
        if metric is None:
            if require_all_metrics:
                failures.append(f"metric_missing: {metric_name}")
        elif metric.rate < minimum_rate:
            failures.append(
                f"{metric_name}: required>={minimum_rate:.2%}, actual={metric.rate:.2%}"
            )

    if (
        profile.maximum_latency_p95_ms is not None
        and report.latency_p95_ms > profile.maximum_latency_p95_ms
    ):
        failures.append(
            "latency_p95_ms: "
            f"required<={profile.maximum_latency_p95_ms:.2f}, "
            f"actual={report.latency_p95_ms:.2f}"
        )
    if (
        profile.maximum_estimated_cost is not None
        and report.estimated_cost > profile.maximum_estimated_cost
    ):
        failures.append(
            "estimated_cost: "
            f"required<={profile.maximum_estimated_cost:.6f}, "
            f"actual={report.estimated_cost:.6f}"
        )

    report.gate_failures = failures
    report.gate_passed = not failures
    return report
