from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from backend.app.agents.after_sales import AfterSalesAgent
from backend.app.agents.contracts import AgentRequest
from backend.app.agents.guardrails import UnsafeInputError, validate_user_input
from backend.app.agents.supervisor import SupervisorAgent
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.evaluation.dataset import DATASET_VERSION
from backend.app.evaluation.fixtures import (
    EvaluationAfterSalesCaseService,
    EvaluationConversationContextService,
    EvaluationCustomerOperationService,
    EvaluationKnowledgeService,
    EvaluationOrderService,
    EvaluationTicketService,
)
from backend.app.evaluation.models import (
    CaseEvaluation,
    EvaluationCase,
    EvaluationObservation,
    EvaluationReport,
    ToolCallObservation,
)
from backend.app.evaluation.scoring import score_case, summarize_metrics
from backend.app.knowledge.embeddings import create_embedding_provider
from backend.app.knowledge.reranker import create_reranker
from backend.app.knowledge.service import KnowledgeService
from backend.app.storage import ObjectStorage
from backend.app.token_budget import estimate_text_tokens, estimate_turn_tokens


_FINGERPRINT_PATHS = (
    "backend/app/agents/supervisor.py",
    "backend/app/agents/after_sales.py",
    "backend/app/agents/tools.py",
    "backend/app/agents/guardrails.py",
    "backend/app/orchestration/workflow.py",
    "backend/app/services/after_sales_rules.py",
)


def system_fingerprint(settings: Settings) -> str:
    root = Path(__file__).resolve().parents[3]
    digest = hashlib.sha256()
    digest.update(settings.kimi_model.encode())
    for relative in _FINGERPRINT_PATHS:
        path = root / relative
        digest.update(relative.encode())
        digest.update(path.read_bytes())
    knowledge_dir = root / "knowledge_docs"
    for path in sorted(knowledge_dir.glob("**/*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 2)


class EvaluationSuiteRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        mode: str = "mock",
        concurrency: int | None = None,
        input_cost_per_million: float = 0,
        output_cost_per_million: float = 0,
    ) -> None:
        if mode not in {"mock", "live-fixture", "live"}:
            raise ValueError("mode must be mock, live-fixture, or live")
        self.mode = mode
        self.settings = settings.model_copy(
            update={
                "agent_mode": "mock" if mode == "mock" else "live",
                "tool_timeout_seconds": 0.05 if mode == "mock" else settings.tool_timeout_seconds,
                "embedding_warmup_on_startup": False,
            }
        )
        self.concurrency = concurrency or (8 if mode == "mock" else 2)
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self._live_database: Database | None = None
        self._live_knowledge: KnowledgeService | None = None

    async def run(self, cases: list[EvaluationCase]) -> EvaluationReport:
        started = time.perf_counter()
        semaphore = asyncio.Semaphore(self.concurrency)

        async def guarded(case: EvaluationCase) -> CaseEvaluation:
            async with semaphore:
                observation = await self._run_case(case)
                return score_case(case, observation, mode=self.mode)

        if self.mode == "live":
            self._live_database = Database(self.settings.database_url)
            self._live_knowledge = KnowledgeService(
                database=self._live_database,
                storage=ObjectStorage(self.settings),
                embeddings=create_embedding_provider(self.settings),
                settings=self.settings,
                reranker=create_reranker(self.settings),
            )
            await self._live_knowledge.warmup()
        try:
            results = list(await asyncio.gather(*(guarded(case) for case in cases)))
        finally:
            if self._live_knowledge is not None:
                await self._live_knowledge.close()
                self._live_knowledge = None
            if self._live_database is not None:
                await self._live_database.dispose()
                self._live_database = None
        metrics = summarize_metrics(results)
        latencies = [item.observation.latency_ms for item in results]
        passed_cases = sum(item.passed for item in results)
        input_tokens = sum(item.observation.estimated_input_tokens for item in results)
        output_tokens = sum(item.observation.estimated_output_tokens for item in results)
        return EvaluationReport(
            dataset_version=DATASET_VERSION,
            mode="mock" if self.mode == "mock" else "live",
            model=self.settings.kimi_model if self.mode == "live" else "mock",
            system_fingerprint=system_fingerprint(self.settings),
            generated_at=datetime.now(UTC).isoformat(),
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            total_cases=len(results),
            passed_cases=passed_cases,
            pass_rate=(passed_cases / len(results)) if results else 1.0,
            category_counts=dict(Counter(item.case.category for item in results)),
            metrics=metrics,
            latency_p50_ms=_percentile(latencies, 0.50),
            latency_p95_ms=_percentile(latencies, 0.95),
            latency_max_ms=round(max(latencies, default=0), 2),
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            estimated_cost=round(
                input_tokens / 1_000_000 * self.input_cost_per_million
                + output_tokens / 1_000_000 * self.output_cost_per_million,
                6,
            ),
            gate_passed=False,
            results=results,
        )

    async def _run_case(self, case: EvaluationCase) -> EvaluationObservation:
        started = time.perf_counter()
        chunks: list[str] = []
        prompt = case.prompt
        try:
            prompt = validate_user_input(prompt)
        except UnsafeInputError as exc:
            response = f"请求已拒绝：{exc}"
            return self._observation(
                case,
                started=started,
                response=response,
                rejected=True,
            )

        orders = EvaluationOrderService(
            case,
            timeout_seconds=self.settings.tool_timeout_seconds,
        )
        knowledge = EvaluationKnowledgeService(
            case,
            timeout_seconds=self.settings.tool_timeout_seconds,
            delegate=self._live_knowledge,
        )
        responder = SupervisorAgent(
            self.settings,
            None,  # ToolRuntime does not access Database directly in this harness.
            knowledge,
            orders,
            EvaluationTicketService(case),
            AfterSalesAgent(self.settings),
            EvaluationAfterSalesCaseService(case),
            conversation_context=EvaluationConversationContextService(case),
            customer_operations=EvaluationCustomerOperationService(case),
        )

        async def on_delta(chunk: str) -> None:
            chunks.append(chunk)

        request = AgentRequest(
            run_id=uuid.uuid5(uuid.NAMESPACE_URL, case.id),
            conversation_id=uuid.uuid5(uuid.NAMESPACE_DNS, case.id),
            customer_id=uuid.UUID(case.customer_id),
            prompt=prompt,
            history=case.history,
        )
        try:
            outcome = await responder.run(request, on_delta)
            return self._observation(
                case,
                started=started,
                response=outcome.final_output,
                trace=outcome.trace,
            )
        except Exception as exc:
            trace = list(getattr(exc, "safe_trace", []))
            return self._observation(
                case,
                started=started,
                response="处理失败，请稍后重试。",
                trace=trace,
                error_type=type(exc).__name__,
            )

    def _observation(
        self,
        case: EvaluationCase,
        *,
        started: float,
        response: str,
        trace: list[dict] | None = None,
        rejected: bool = False,
        error_type: str | None = None,
    ) -> EvaluationObservation:
        trace = trace or []
        plan = next(
            (item.get("plan") for item in trace if item.get("event") == "supervisor_plan_completed"),
            None,
        )
        workflow_result = next(
            (item.get("result") for item in trace if item.get("event") == "workflow_completed"),
            None,
        )
        tools = [
            ToolCallObservation(
                name=str(item.get("tool_name")),
                arguments=dict(item.get("arguments") or {}),
            )
            for item in trace
            if item.get("event") == "tool_started"
        ]
        initial_input_tokens = estimate_text_tokens(case.prompt) + sum(
            estimate_turn_tokens(turn) for turn in case.history
        )
        plan_text = json.dumps(plan, ensure_ascii=False, default=str) if plan else ""
        workflow_text = (
            json.dumps(workflow_result, ensure_ascii=False, default=str)
            if workflow_result
            else ""
        )
        # Live execution sends the user input to the planner and then sends the user input plus
        # the structured workflow result to the response model. Including structured plan and
        # after-sales output makes this a conservative estimate when provider usage is absent.
        input_tokens = initial_input_tokens
        if workflow_result:
            input_tokens += estimate_text_tokens(case.prompt) + estimate_text_tokens(workflow_text)
        output_tokens = estimate_text_tokens(response) + estimate_text_tokens(plan_text)
        if isinstance(workflow_result, dict) and workflow_result.get("after_sales"):
            output_tokens += estimate_text_tokens(
                json.dumps(workflow_result["after_sales"], ensure_ascii=False, default=str)
            )
        estimated_cost = (
            input_tokens / 1_000_000 * self.input_cost_per_million
            + output_tokens / 1_000_000 * self.output_cost_per_million
        )
        return EvaluationObservation(
            case_id=case.id,
            response=response,
            rejected=rejected,
            intent=plan.get("intent") if isinstance(plan, dict) else None,
            tools=tools,
            workflow_result=workflow_result if isinstance(workflow_result, dict) else None,
            trace=trace,
            error_type=error_type,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            estimated_cost=round(estimated_cost, 8),
        )
