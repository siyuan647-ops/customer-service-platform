from __future__ import annotations

import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.main import create_app
from backend.app.messaging import AGENT_REPLY_REQUESTED
from backend.app.messaging.outbox import AgentReplyOutboxRelay
from backend.app.messaging.rabbitmq import RabbitCommandBus
from backend.app.models import AgentRun, Conversation, Message, OutboxEvent
from backend.app.orders.demo_data import DEMO_CUSTOMER_ID


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "app_env": "test",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}",
        "event_backend": "memory",
        "minio_enabled": False,
        "agent_mode": "mock",
        "order_backend": "mock",
        "embedding_mode": "hash",
        "auto_create_schema": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_rabbitmq_mode_persists_command_without_running_agent(tmp_path):
    conversation_id = uuid.uuid4()
    with TestClient(
        create_app(_settings(tmp_path, agent_task_backend="rabbitmq"))
    ) as client:
        response = client.post(
            f"/conversations/{conversation_id}/messages",
            headers={"X-Customer-ID": DEMO_CUSTOMER_ID},
            json={"content": "查询订单 ORD-20260918-001"},
        )
        conversation = client.get(
            f"/conversations/{conversation_id}",
            headers={"X-Customer-ID": DEMO_CUSTOMER_ID},
        )

    assert response.status_code == 202
    assert [item["role"] for item in conversation.json()["messages"]] == ["user"]
    with sqlite3.connect(tmp_path / "queue.db") as connection:
        run = connection.execute(
            "SELECT status, input_message_id FROM agent_runs"
        ).fetchone()
        event = connection.execute(
            "SELECT event_type, status, payload FROM outbox_events"
        ).fetchone()
    assert run is not None and run[0] == "queued"
    assert event is not None
    assert event[0:2] == (AGENT_REPLY_REQUESTED, "PENDING")
    assert "查询订单" not in event[2]


def test_inline_mode_marks_agent_command_as_published(tmp_path):
    conversation_id = uuid.uuid4()
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post(
            f"/conversations/{conversation_id}/messages",
            headers={"X-Customer-ID": DEMO_CUSTOMER_ID},
            json={"content": "查询订单 ORD-20260918-001"},
        )

    assert response.status_code == 202
    with sqlite3.connect(tmp_path / "queue.db") as connection:
        run_status = connection.execute("SELECT status FROM agent_runs").fetchone()[0]
        outbox_status = connection.execute(
            "SELECT status FROM outbox_events WHERE event_type = ?",
            (AGENT_REPLY_REQUESTED,),
        ).fetchone()[0]
        assistant_count = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE role = 'assistant'"
        ).fetchone()[0]
    assert run_status == "completed"
    assert outbox_status == "PUBLISHED"
    assert assistant_count == 1


@pytest.mark.asyncio
async def test_first_consumer_retry_uses_five_second_tier(tmp_path):
    bus = RabbitCommandBus(_settings(tmp_path))
    published: list[tuple[str, dict]] = []

    async def record(routing_key, payload, **metadata):
        published.append((routing_key, metadata))

    bus.publish = record  # type: ignore[method-assign]
    await bus.publish_retry(
        {"event_type": AGENT_REPLY_REQUESTED},
        message_id="event-1",
        correlation_id="run-1",
        attempt=2,
    )

    assert published == [
        (
            "agent.reply.retry.1",
            {
                "message_id": "event-1",
                "correlation_id": "run-1",
                "headers": {"x-agent-attempt": 2},
            },
        )
    ]


class RecordingPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[tuple[str, dict, dict]] = []

    async def publish(self, routing_key, payload, **metadata) -> None:
        if self.fail:
            raise ConnectionError("RabbitMQ unavailable")
        self.messages.append((routing_key, payload, metadata))


class RecordingBroker:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def publish(self, conversation_id, event, data):
        self.events.append((conversation_id, event, data))
        return "1-0"


async def _seed_outbox(database: Database) -> tuple[uuid.UUID, uuid.UUID]:
    conversation_id = uuid.uuid4()
    message_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with database.session_factory() as session:
        session.add(
            Conversation(
                id=conversation_id,
                customer_id=uuid.UUID(DEMO_CUSTOMER_ID),
                status="processing",
                channel="web",
            )
        )
        session.add(
            Message(
                id=message_id,
                conversation_id=conversation_id,
                role="user",
                content="hello",
                status="completed",
            )
        )
        session.add(
            AgentRun(
                id=run_id,
                conversation_id=conversation_id,
                input_message_id=message_id,
                status="queued",
                model="mock",
                trace=[],
            )
        )
        session.add(
            OutboxEvent(
                aggregate_type="agent_run",
                aggregate_id=run_id,
                event_type=AGENT_REPLY_REQUESTED,
                payload={
                    "run_id": str(run_id),
                    "conversation_id": str(conversation_id),
                    "input_message_id": str(message_id),
                },
                idempotency_key=f"agent-reply:{run_id}",
                status="PENDING",
            )
        )
        await session.commit()
    return conversation_id, run_id


@pytest.mark.asyncio
async def test_outbox_relay_publishes_and_marks_event(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'relay.db'}")
    await database.create_schema()
    _, run_id = await _seed_outbox(database)
    publisher = RecordingPublisher()
    relay = AgentReplyOutboxRelay(
        database,
        publisher,
        max_attempts=3,
        lock_timeout_seconds=60,
    )

    assert await relay.process_once() is True
    assert len(publisher.messages) == 1
    routing_key, envelope, metadata = publisher.messages[0]
    assert routing_key == AGENT_REPLY_REQUESTED
    assert envelope["payload"]["run_id"] == str(run_id)
    assert metadata["headers"] == {"x-agent-attempt": 1}
    async with database.session_factory() as session:
        event = await session.scalar(select(OutboxEvent))
    assert event is not None and event.status == "PUBLISHED"
    assert event.processed_at is not None
    await database.dispose()


@pytest.mark.asyncio
async def test_outbox_relay_exhaustion_fails_run_and_notifies_frontend(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'relay-fail.db'}")
    await database.create_schema()
    conversation_id, _ = await _seed_outbox(database)
    publisher = RecordingPublisher(fail=True)
    broker = RecordingBroker()
    relay = AgentReplyOutboxRelay(
        database,
        publisher,
        max_attempts=1,
        lock_timeout_seconds=60,
        failure_broker=broker,
    )

    assert await relay.process_once() is True
    async with database.session_factory() as session:
        event = await session.scalar(select(OutboxEvent))
        run = await session.scalar(select(AgentRun))
    assert event is not None and event.status == "FAILED"
    assert event.last_error == "ConnectionError"
    assert run is not None and run.status == "failed"
    assert broker.events == [
        (
            str(conversation_id),
            "assistant.failed",
            {"run_id": str(run.id), "error": "ConnectionError"},
        )
    ]
    await database.dispose()
