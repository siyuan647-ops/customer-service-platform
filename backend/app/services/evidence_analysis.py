from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from backend.app.agents.evidence import EvidenceAnalyzer
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.models import (
    AfterSalesActionLog,
    AfterSalesCase,
    AfterSalesEvidence,
    OutboxEvent,
)
from backend.app.orders.gateway import OrderGateway, OrderGatewayError
from backend.app.security.circuit_breaker import CircuitOpenError
from backend.app.storage import ObjectStorage


EVIDENCE_ANALYSIS_EVENT = "after_sales.evidence.analyze"


def _normalize_tracking_number(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


class EvidenceAnalysisService:
    def __init__(
        self,
        database: Database,
        storage: ObjectStorage,
        orders: OrderGateway,
        analyzer: EvidenceAnalyzer,
        settings: Settings,
    ) -> None:
        self.database = database
        self.storage = storage
        self.orders = orders
        self.analyzer = analyzer
        self.settings = settings

    async def process_once(self) -> bool:
        event_id = await self._claim_event()
        if event_id is None:
            return False
        try:
            await self._process_event(event_id)
        except CircuitOpenError as exc:
            await self._defer_open_circuit(event_id, exc.retry_after_seconds)
        except Exception as exc:
            await self._mark_event_failed(event_id, exc)
        return True

    async def _defer_open_circuit(self, event_id: uuid.UUID, retry_after: int) -> None:
        async with self.database.session_factory() as session:
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if event is None:
                return
            event.status = "PENDING"
            event.available_at = datetime.now(UTC) + timedelta(seconds=max(1, retry_after))
            event.locked_at = None
            event.attempt_count = max(0, event.attempt_count - 1)
            event.last_error = "CircuitOpenError"
            evidence = await session.get(
                AfterSalesEvidence, uuid.UUID(str(event.payload["evidence_id"])),
                with_for_update=True,
            )
            if evidence is not None:
                evidence.analysis_status = "PENDING"
                evidence.analysis_error = None
            await session.commit()

    async def _claim_event(self) -> uuid.UUID | None:
        now = datetime.now(UTC)
        async with self.database.session_factory() as session:
            event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type == EVIDENCE_ANALYSIS_EVENT,
                    OutboxEvent.status == "PENDING",
                    OutboxEvent.available_at <= now,
                )
                .order_by(OutboxEvent.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                return None
            event.status = "PROCESSING"
            event.locked_at = now
            event.attempt_count += 1
            evidence = await session.get(
                AfterSalesEvidence,
                uuid.UUID(str(event.payload["evidence_id"])),
                with_for_update=True,
            )
            if evidence is not None:
                evidence.analysis_status = "PROCESSING"
                evidence.analysis_error = None
            await session.commit()
            return event.id

    async def _process_event(self, event_id: uuid.UUID) -> None:
        async with self.database.session_factory() as session:
            event = await session.get(OutboxEvent, event_id)
            if event is None or event.status != "PROCESSING":
                return
            evidence_id = uuid.UUID(str(event.payload["evidence_id"]))
            evidence = await session.get(AfterSalesEvidence, evidence_id)
            if evidence is None:
                object_key = None
                content_type = None
                filename = None
                case_id = None
            else:
                object_key = evidence.object_key
                content_type = evidence.content_type
                filename = evidence.filename
                case_id = evidence.case_id

        if object_key is None or content_type is None or filename is None or case_id is None:
            await self._mark_processed(event_id)
            return

        data = await self.storage.get_bytes(object_key)
        result = await self.analyzer.analyze(
            data=data,
            content_type=content_type,
            filename=filename,
        )
        result_data = result.model_dump(mode="json")
        result_data["tracking_number_match"] = await self._tracking_number_match(
            case_id,
            result.extracted_tracking_number,
        )

        now = datetime.now(UTC)
        async with self.database.session_factory() as session:
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            evidence = await session.get(
                AfterSalesEvidence, evidence_id, with_for_update=True
            )
            if event is None or evidence is None:
                return
            case = await session.get(AfterSalesCase, evidence.case_id)
            case_status = case.status if case is not None else "UNKNOWN"
            evidence.analysis_status = "COMPLETED"
            evidence.analysis_result = result_data
            evidence.analysis_model = self.settings.kimi_model
            evidence.analysis_prompt_version = (
                self.settings.evidence_analysis_prompt_version
            )
            evidence.analysis_error = None
            evidence.analyzed_at = now
            session.add(
                AfterSalesActionLog(
                    case_id=evidence.case_id,
                    from_status=case_status,
                    to_status=case_status,
                    action="evidence_analysis_completed",
                    actor_type="system",
                    actor_id="after-sales-worker",
                    action_metadata={"evidence_id": str(evidence.id)},
                )
            )
            event.status = "PROCESSED"
            event.processed_at = now
            event.locked_at = None
            event.last_error = None
            await session.commit()

    async def _tracking_number_match(
        self,
        case_id: uuid.UUID,
        extracted_tracking_number: str | None,
    ) -> bool | None:
        if not extracted_tracking_number:
            return None
        async with self.database.session_factory() as session:
            case = await session.get(AfterSalesCase, case_id)
        if case is None:
            return None
        try:
            order = await self.orders.get_order(case.order_no, case.customer_id)
        except (OrderGatewayError, CircuitOpenError):
            return None
        if order is None:
            return None
        expected = {
            _normalize_tracking_number(item.tracking_number)
            for item in order.shipments
            if item.tracking_number
        }
        if not expected:
            return None
        return _normalize_tracking_number(extracted_tracking_number) in expected

    async def _mark_processed(self, event_id: uuid.UUID) -> None:
        async with self.database.session_factory() as session:
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if event is not None:
                event.status = "PROCESSED"
                event.processed_at = datetime.now(UTC)
                event.locked_at = None
                event.last_error = None
                await session.commit()

    async def _mark_event_failed(self, event_id: uuid.UUID, exc: Exception) -> None:
        now = datetime.now(UTC)
        # Provider errors may contain request details. Keep only the exception
        # class in persistence; full customer media and credentials are never
        # written to logs or traces by this service.
        error = type(exc).__name__
        async with self.database.session_factory() as session:
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if event is None:
                return
            exhausted = (
                event.attempt_count >= self.settings.after_sales_outbox_max_attempts
            )
            event.status = "FAILED" if exhausted else "PENDING"
            event.available_at = now + timedelta(
                seconds=min(60, 2 ** max(event.attempt_count, 1))
            )
            event.locked_at = None
            event.last_error = error
            evidence = await session.get(
                AfterSalesEvidence,
                uuid.UUID(str(event.payload["evidence_id"])),
                with_for_update=True,
            )
            if evidence is not None:
                evidence.analysis_status = "FAILED" if exhausted else "PENDING"
                evidence.analysis_error = error
            await session.commit()
