from __future__ import annotations

import json
from pathlib import Path

from backend.app.evaluation.models import EvaluationReport


def report_markdown(report: EvaluationReport) -> str:
    gate = "PASS" if report.gate_passed else "FAIL"
    lines = [
        f"# Agent evaluation report: {gate}",
        "",
        f"- Dataset: `{report.dataset_version}`",
        f"- Mode / model: `{report.mode}` / `{report.model}`",
        f"- System fingerprint: `{report.system_fingerprint}`",
        f"- Cases: {report.passed_cases}/{report.total_cases} ({report.pass_rate:.2%})",
        f"- Duration: {report.duration_ms:.2f} ms",
        (
            "- Latency p50 / p95 / max: "
            f"{report.latency_p50_ms:.2f} / {report.latency_p95_ms:.2f} / "
            f"{report.latency_max_ms:.2f} ms"
        ),
        (
            "- Estimated tokens (input / output): "
            f"{report.estimated_input_tokens} / {report.estimated_output_tokens}"
        ),
        f"- Estimated cost: {report.estimated_cost:.6f}",
        "",
        "## Metrics",
        "",
        "| Check | Passed | Total | Rate |",
        "|---|---:|---:|---:|",
    ]
    for name, metric in report.metrics.items():
        lines.append(f"| `{name}` | {metric.passed} | {metric.total} | {metric.rate:.2%} |")

    lines.extend(["", "## Quality gate", ""])
    if report.gate_failures:
        lines.extend(f"- {failure}" for failure in report.gate_failures)
    else:
        lines.append("All configured thresholds passed.")

    failed = [result for result in report.results if not result.passed]
    lines.extend(["", f"## Failed cases ({len(failed)})", ""])
    if not failed:
        lines.append("No failed cases.")
    for result in failed:
        observation = result.observation
        lines.extend(
            [
                f"### `{result.case.id}` — {result.case.category}",
                "",
                f"- Prompt: {result.case.prompt}",
                f"- Fixture: `{result.case.fixture}`",
                f"- Intent: expected `{result.case.expected.intent}`, actual `{observation.intent}`",
                "- Tools: `"
                + ", ".join(call.name for call in observation.tools)
                + "`",
                f"- Response: {observation.response}",
                "- Failed checks:",
                "",
            ]
        )
        for check in result.checks:
            if check.applicable and not check.passed:
                lines.append(f"  - `{check.name}`: {check.detail}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: EvaluationReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "latest.json"
    markdown_path = output_dir / "latest.md"
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(report_markdown(report), encoding="utf-8")
    return json_path, markdown_path
