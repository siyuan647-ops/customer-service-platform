from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog
from sqlalchemy import or_, select

from backend.app.database import Database
from backend.app.events import EventBroker
from backend.app.messaging.rabbitmq import AGENT_REPLY_REQUESTED
from backend.app.models import AgentRun, Conversation, OutboxEvent


logger = structlog.get_logger()


class CommandPublisher(Protocol):
    async def publish(
        self,
        routing_key: str,
        payload: dict,
        *,
        message_id: str,
        correlation_id: str | None = None,
        headers: dict | None = None,
    ) -> None: ...


@dataclass(slots=True)
class ClaimedOutboxEvent:
    id: uuid.UUID
    aggregate_type: str
    aggregate_id: uuid.UUID
    event_type: str
    payload: dict
    created_at: datetime
    attempt_count: int


class AgentReplyOutboxRelay:
    """Publishes agent reply requests from PostgreSQL to RabbitMQ."""

    def __init__(
        self,
        database: Database,
        publisher: CommandPublisher,
        *,
        max_attempts: int,
        lock_timeout_seconds: int,
        failure_broker: EventBroker | None = None,
    ) -> None:
        self.database = database
        self.publisher = publisher
        self.max_attempts = max_attempts
        self.lock_timeout_seconds = lock_timeout_seconds
        self.failure_broker = failure_broker

    async def recover_stale_claims(self) -> int:
        stale_before = datetime.now(UTC) - timedelta(seconds=self.lock_timeout_seconds)
        recovered = 0
        async with self.database.session_factory() as session:
            events = await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == AGENT_REPLY_REQUESTED,
                    OutboxEvent.status == "PUBLISHING",
                    or_(
                        OutboxEvent.locked_at.is_(None),
                        OutboxEvent.locked_at < stale_before,
                    ),
                )
            )
            for event in events:
                event.status = "PENDING"
                event.locked_at = None
                recovered += 1
            if recovered:
                await session.commit()
        return recovered

    async def process_once(self) -> bool:
        event = await self._claim_one()
        if event is None:
            return False
        envelope = {
            "event_id": str(event.id),
            "event_type": event.event_type,
            "occurred_at": event.created_at.isoformat(),
            "aggregate_type": event.aggregate_type,
            "aggregate_id": str(event.aggregate_id),
            "correlation_id": str(event.payload["run_id"]),
            "payload": event.payload,
        }
        try:
            await self.publisher.publish(
                event.event_type,
                envelope,
                message_id=str(event.id),
                correlation_id=str(event.payload["run_id"]),
                headers={"x-agent-attempt": 1},
            )
        except Exception as exc:
            exhausted = await self._mark_publish_failed(event, exc)
            if exhausted:
                await self._publish_terminal_failure(event, type(exc).__name__)
            return True
        await self._mark_published(event.id)
        return True

    async def _claim_one(self) -> ClaimedOutboxEvent | None:
        now = datetime.now(UTC)
        async with self.database.session_factory() as session:
            event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type == AGENT_REPLY_REQUESTED,
                    OutboxEvent.status == "PENDING",
                    OutboxEvent.available_at <= now,
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                return None
            event.status = "PUBLISHING"
            event.locked_at = now
            event.attempt_count += 1
            claimed = ClaimedOutboxEvent(
                id=event.id,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                event_type=event.event_type,
                payload=dict(event.payload),
                created_at=event.created_at,
                attempt_count=event.attempt_count,
            )
            await session.commit()
            return claimed

    async def _mark_published(self, event_id: uuid.UUID) -> None:
        async with self.database.session_factory() as session:
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if event is None or event.status != "PUBLISHING":
                return
            event.status = "PUBLISHED"
            event.processed_at = datetime.now(UTC)
            event.locked_at = None
            event.last_error = None
            await session.commit()

    async def _mark_publish_failed(
        self,
        event: ClaimedOutboxEvent,
        exc: Exception,
    ) -> bool:
        now = datetime.now(UTC)
        exhausted = event.attempt_count >= self.max_attempts
        async with self.database.session_factory() as session:
            row = await session.get(OutboxEvent, event.id, with_for_update=True)
            if row is None:
                return exhausted
            row.status = "FAILED" if exhausted else "PENDING"
            row.available_at = now + timedelta(
                seconds=min(60, 2 ** max(event.attempt_count, 1))
            )
            row.locked_at = None
            row.last_error = type(exc).__name__
            if exhausted:
                run = await session.get(
                    AgentRun,
                    uuid.UUID(str(event.payload["run_id"])),
                    with_for_update=True,
                )
                if run is not None and run.status not in {"completed", "failed"}:
                    run.status = "failed"
                    run.error_type = "RabbitPublishExhausted"
                    run.completed_at = now
                    conversation = await session.get(Conversation, run.conversation_id)
                    if conversation is not None:
                        conversation.status = "active"
            await session.commit()
        logger.warning(
            "agent_reply_outbox_publish_failed",
            event_id=str(event.id),
            attempt=event.attempt_count,
            exhausted=exhausted,
            error_type=type(exc).__name__,
        )
        return exhausted

    async def _publish_terminal_failure(
        self,
        event: ClaimedOutboxEvent,
        error_type: str,
    ) -> None:
        if self.failure_broker is None:
            return
        try:
            await self.failure_broker.publish(
                str(event.payload["conversation_id"]),
                "assistant.failed",
                {"run_id": str(event.payload["run_id"]), "error": error_type},
            )
        except Exception:
            logger.exception(
                "agent_reply_publish_failure_notification_failed",
                event_id=str(event.id),
            )
