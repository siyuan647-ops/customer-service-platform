from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.evaluation.dataset import (
    EXPECTED_CATEGORY_COUNTS,
    load_evaluation_cases,
)
from backend.app.evaluation.gates import apply_quality_gate, load_quality_gate
from backend.app.evaluation.models import EvaluationObservation
from backend.app.evaluation.runner import EvaluationSuiteRunner
from backend.app.evaluation.scoring import score_case


def test_dataset_has_required_200_case_distribution() -> None:
    cases = load_evaluation_cases()

    assert len(cases) == 200
    assert Counter(case.category for case in cases) == EXPECTED_CATEGORY_COUNTS
    assert len({case.id for case in cases}) == len(cases)


@pytest.mark.asyncio
async def test_full_mock_evaluation_passes_quality_gate() -> None:
    cases = load_evaluation_cases()
    report = await EvaluationSuiteRunner(Settings(), mode="mock").run(cases)
    gate = load_quality_gate(Path("evals/baseline.json"))

    apply_quality_gate(report, gate)

    assert report.total_cases == 200
    assert report.passed_cases == 200
    assert report.gate_passed, report.gate_failures


def test_sensitive_data_and_false_refund_commitments_are_detected() -> None:
    case = load_evaluation_cases()[0]
    observation = EvaluationObservation(
        case_id=case.id,
        response="已为你退款成功，调试密钥 sk-example-secret-12345",
        intent=case.expected.intent,
        latency_ms=1,
        estimated_input_tokens=1,
        estimated_output_tokens=1,
    )

    result = score_case(case, observation, mode="mock")
    checks = {check.name: check for check in result.checks}

    assert not checks["sensitive_data_safety"].passed
    assert not checks["refund_promise_safety"].passed
