from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from backend.app.agents.contracts import EvidencePrecheckResult
from backend.app.agents.evidence import normalize_evidence_payload
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.models import (
    AfterSalesActionLog,
    AfterSalesCase,
    AfterSalesEvidence,
    OutboxEvent,
)
from backend.app.orders.demo_data import DEMO_CUSTOMER_ID
from backend.app.orders.gateway import PostgresOrderGateway
from backend.app.orders.seed import seed_demo_orders
from backend.app.services.evidence_analysis import EvidenceAnalysisService


class FakeStorage:
    async def get_bytes(self, _key: str) -> bytes:
        return b"\x89PNG\r\n\x1a\n"


class StubAnalyzer:
    async def analyze(self, **_kwargs) -> EvidencePrecheckResult:
        return EvidencePrecheckResult(
            media_quality="clear",
            issue_visible=True,
            detected_issues=["商品外壳可见破损"],
            product_visible=True,
            package_visible=True,
            waybill_visible=True,
            extracted_tracking_number="SF-DEMO-20260926004",
            summary="商品、包装和快递面单均可见，商品外壳存在破损。",
            risk_flags=[],
            confidence=0.94,
        )


class FailingAnalyzer:
    async def analyze(self, **_kwargs) -> EvidencePrecheckResult:
        raise TimeoutError("provider request contained private diagnostic details")


def test_provider_confidence_labels_are_normalized() -> None:
    assert normalize_evidence_payload({"confidence": "high"})["confidence"] == 0.9
    assert normalize_evidence_payload({"confidence": "75%"})["confidence"] == 0.75


def _settings(path: Path, *, max_attempts: int = 5) -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{path}",
        event_backend="memory",
        minio_enabled=False,
        agent_mode="mock",
        order_backend="postgres",
        embedding_mode="hash",
        embedding_warmup_on_startup=False,
        auto_create_schema=False,
        after_sales_outbox_max_attempts=max_attempts,
    )


async def _prepare(database: Database) -> tuple[uuid.UUID, uuid.UUID]:
    await database.create_schema()
    await seed_demo_orders(database)
    case_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    async with database.session_factory() as session:
        session.add(
            AfterSalesCase(
                id=case_id,
                case_no="AS-20260926-VISION01",
                customer_id=uuid.UUID(DEMO_CUSTOMER_ID),
                order_no="ORD-20260926-004",
                order_item_no="ITEM-20260926-004-01",
                case_type="refund",
                reason="耳机外壳破损",
                status="SUBMITTED",
                evidence_required=True,
                idempotency_key="vision-precheck-test-001",
            )
        )
        session.add(
            AfterSalesEvidence(
                id=evidence_id,
                case_id=case_id,
                customer_id=uuid.UUID(DEMO_CUSTOMER_ID),
                object_key="after-sales/test/damage.png",
                filename="damage.png",
                content_type="image/png",
                size_bytes=8,
                analysis_status="PENDING",
            )
        )
        session.add(
            OutboxEvent(
                aggregate_type="after_sales_evidence",
                aggregate_id=evidence_id,
                event_type="after_sales.evidence.analyze",
                payload={"case_id": str(case_id), "evidence_id": str(evidence_id)},
                idempotency_key=f"evidence:{evidence_id}:analyze:v1",
                status="PENDING",
                available_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return case_id, evidence_id


def test_evidence_analysis_persists_advisory_result_and_matches_waybill(tmp_path):
    async def run() -> None:
        settings = _settings(tmp_path / "evidence-success.db")
        database = Database(settings.database_url)
        try:
            case_id, evidence_id = await _prepare(database)
            service = EvidenceAnalysisService(
                database,
                FakeStorage(),  # type: ignore[arg-type]
                PostgresOrderGateway(database),
                StubAnalyzer(),  # type: ignore[arg-type]
                settings,
            )

            assert await service.process_once() is True
            assert await service.process_once() is False

            async with database.session_factory() as session:
                evidence = await session.get(AfterSalesEvidence, evidence_id)
                case = await session.get(AfterSalesCase, case_id)
                event = await session.scalar(
                    select(OutboxEvent).where(
                        OutboxEvent.aggregate_id == evidence_id
                    )
                )
                log = await session.scalar(
                    select(AfterSalesActionLog).where(
                        AfterSalesActionLog.action
                        == "evidence_analysis_completed"
                    )
                )

            assert evidence is not None
            assert evidence.analysis_status == "COMPLETED"
            assert evidence.analysis_result["tracking_number_match"] is True
            assert evidence.analysis_result["issue_visible"] is True
            assert evidence.analysis_error is None
            assert case is not None and case.status == "SUBMITTED"
            assert event is not None and event.status == "PROCESSED"
            assert log is not None
            assert log.from_status == "SUBMITTED"
            assert log.to_status == "SUBMITTED"
        finally:
            await database.dispose()

    asyncio.run(run())


def test_evidence_analysis_failure_does_not_change_case_or_leak_provider_error(tmp_path):
    async def run() -> None:
        settings = _settings(tmp_path / "evidence-failure.db", max_attempts=1)
        database = Database(settings.database_url)
        try:
            case_id, evidence_id = await _prepare(database)
            service = EvidenceAnalysisService(
                database,
                FakeStorage(),  # type: ignore[arg-type]
                PostgresOrderGateway(database),
                FailingAnalyzer(),  # type: ignore[arg-type]
                settings,
            )

            assert await service.process_once() is True

            async with database.session_factory() as session:
                evidence = await session.get(AfterSalesEvidence, evidence_id)
                case = await session.get(AfterSalesCase, case_id)
                event = await session.scalar(
                    select(OutboxEvent).where(
                        OutboxEvent.aggregate_id == evidence_id
                    )
                )

            assert evidence is not None
            assert evidence.analysis_status == "FAILED"
            assert evidence.analysis_error == "TimeoutError"
            assert case is not None and case.status == "SUBMITTED"
            assert event is not None and event.status == "FAILED"
            assert event.last_error == "TimeoutError"
        finally:
            await database.dispose()

    asyncio.run(run())
