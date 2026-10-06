from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from backend.app.config import get_settings
from backend.app.evaluation.dataset import EXPECTED_CATEGORY_COUNTS, load_evaluation_cases
from backend.app.evaluation.gates import apply_quality_gate, load_quality_gate
from backend.app.evaluation.reporting import write_report
from backend.app.evaluation.runner import EvaluationSuiteRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the customer-service agent eval suite")
    parser.add_argument(
        "--mode",
        choices=("mock", "live-fixture", "live"),
        default="mock",
        help=(
            "mock: no model/network calls; live-fixture: real Kimi with deterministic "
            "knowledge fixtures; live: real Kimi plus the configured PostgreSQL/pgvector "
            "knowledge retrieval"
        ),
    )
    parser.add_argument(
        "--category",
        action="append",
        choices=tuple(EXPECTED_CATEGORY_COUNTS),
        help="Run one or more categories (repeat the option).",
    )
    parser.add_argument("--case-id", action="append", help="Run specific case IDs.")
    parser.add_argument("--limit", type=int, help="Limit cases after filtering.")
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--baseline", type=Path, default=Path("evals/baseline.json"))
    parser.add_argument("--report-dir", type=Path, default=Path("artifacts/evaluation"))
    parser.add_argument("--input-cost-per-million", type=float, default=0)
    parser.add_argument("--output-cost-per-million", type=float, default=0)
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.mode != "mock" and not settings.kimi_api_key:
        raise SystemExit("Live evaluation requires KIMI_API_KEY in the environment or .env")

    all_cases = load_evaluation_cases()
    cases = all_cases
    if args.category:
        categories = set(args.category)
        cases = [case for case in cases if case.category in categories]
    if args.case_id:
        requested = set(args.case_id)
        cases = [case for case in cases if case.id in requested]
        missing = requested - {case.id for case in cases}
        if missing:
            raise SystemExit(f"Unknown case IDs: {', '.join(sorted(missing))}")
    if args.limit is not None:
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("No evaluation cases selected")

    runner = EvaluationSuiteRunner(
        settings,
        mode=args.mode,
        concurrency=args.concurrency,
        input_cost_per_million=args.input_cost_per_million,
        output_cost_per_million=args.output_cost_per_million,
    )
    report = await runner.run(cases)
    report = apply_quality_gate(
        report,
        load_quality_gate(args.baseline),
        require_all_metrics=len(cases) == len(all_cases),
    )
    json_path, markdown_path = write_report(report, args.report_dir)
    gate = "PASS" if report.gate_passed else "FAIL"
    print(
        f"[{gate}] {report.passed_cases}/{report.total_cases} cases "
        f"({report.pass_rate:.2%}); p95={report.latency_p95_ms:.2f} ms; "
        f"estimated_tokens={report.estimated_input_tokens + report.estimated_output_tokens}"
    )
    print(f"JSON report: {json_path.resolve()}")
    print(f"Markdown report: {markdown_path.resolve()}")
    for failure in report.gate_failures:
        print(f"GATE: {failure}")
    return 0 if report.gate_passed else 1


def main() -> None:
    args = build_parser().parse_args()
    try:
        exit_code = asyncio.run(_run(args))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
