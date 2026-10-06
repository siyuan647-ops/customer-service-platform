from __future__ import annotations

import asyncio
import json
import signal
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from aio_pika import IncomingMessage

from backend.app.agents.after_sales import AfterSalesAgent
from backend.app.agents.supervisor import SupervisorAgent
from backend.app.config import Settings, get_settings
from backend.app.conversation_memory import create_conversation_memory
from backend.app.database import Database
from backend.app.events import create_event_broker
from backend.app.knowledge.embeddings import create_embedding_provider
from backend.app.knowledge.reranker import create_reranker
from backend.app.knowledge.service import KnowledgeService
from backend.app.logging import configure_logging
from backend.app.messaging.locks import ConversationLeaseManager
from backend.app.messaging.rabbitmq import AGENT_REPLY_REQUESTED, RabbitCommandBus
from backend.app.models import AgentRun, Conversation
from backend.app.orders import create_order_gateway
from backend.app.integrations import MockInvoiceAdapter, MockOmsAdapter
from backend.app.services.customer_operations import CustomerOperationService
from backend.app.services.after_sales_cases import AfterSalesCaseService
from backend.app.services.conversations import process_assistant_reply
from backend.app.services.order import OrderService
from backend.app.services.tickets import TicketService
from backend.app.storage import ObjectStorage
from backend.app.security.circuit_breaker import CircuitOpenError, RedisCircuitRegistry


logger = structlog.get_logger()


def _parse_command(message: IncomingMessage) -> tuple[dict[str, Any], uuid.UUID, str]:
    payload = json.loads(message.body.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("event_type") != AGENT_REPLY_REQUESTED:
        raise ValueError("Unexpected RabbitMQ command type")
    command_payload = payload.get("payload")
    if not isinstance(command_payload, dict):
        raise ValueError("RabbitMQ command payload is missing")
    run_id = uuid.UUID(str(command_payload["run_id"]))
    conversation_id = str(uuid.UUID(str(command_payload["conversation_id"])))
    return payload, run_id, conversation_id


def _attempt(message: IncomingMessage) -> int:
    raw = (message.headers or {}).get("x-agent-attempt", 1)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


class AgentReplyConsumer:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        broker,
        memory,
        responder: SupervisorAgent,
        command_bus: RabbitCommandBus,
        leases: ConversationLeaseManager,
    ) -> None:
        self.settings = settings
        self.database = database
        self.broker = broker
        self.memory = memory
        self.responder = responder
        self.command_bus = command_bus
        self.leases = leases

    async def handle(self, message: IncomingMessage) -> None:
        attempt = _attempt(message)
        message_id = message.message_id or str(uuid.uuid4())
        correlation_id = message.correlation_id
        try:
            payload, run_id, conversation_id = _parse_command(message)
            correlation_id = correlation_id or str(run_id)
        except Exception as exc:
            try:
                decoded = json.loads(message.body.decode("utf-8"))
                dead_payload = decoded if isinstance(decoded, dict) else {"raw": "invalid"}
                await self.command_bus.publish_dead_letter(
                    dead_payload,
                    message_id=message_id,
                    correlation_id=correlation_id,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
                await message.ack()
            except Exception:
                await message.nack(requeue=True)
            return

        async with self.leases.acquire(conversation_id) as acquired:
            if not acquired:
                try:
                    await self.command_bus.publish_retry(
                        payload,
                        message_id=message_id,
                        correlation_id=correlation_id,
                        attempt=1,
                    )
                    await message.ack()
                except Exception:
                    await message.nack(requeue=True)
                return

            final_attempt = attempt >= self.settings.rabbitmq_consumer_max_attempts
            try:
                result = await process_assistant_reply(
                    database=self.database,
                    broker=self.broker,
                    memory=self.memory,
                    responder=self.responder,
                    run_id=run_id,
                    final_attempt=final_attempt,
                )
                if result == "failed":
                    await self.command_bus.publish_dead_letter(
                        payload,
                        message_id=message_id,
                        correlation_id=correlation_id,
                        attempt=attempt,
                        error_type="AgentReplyFailed",
                    )
                await message.ack()
            except CircuitOpenError:
                try:
                    await self.command_bus.publish_retry(
                        payload, message_id=message_id, correlation_id=correlation_id,
                        attempt=attempt, delay_tier=2,
                    )
                    await message.ack()
                except Exception:
                    await message.nack(requeue=True)
            except Exception as exc:
                try:
                    if final_attempt:
                        await self._mark_terminal_failure(run_id, type(exc).__name__)
                        await self.command_bus.publish_dead_letter(
                            payload,
                            message_id=message_id,
                            correlation_id=correlation_id,
                            attempt=attempt,
                            error_type=type(exc).__name__,
                        )
                    else:
                        await self.command_bus.publish_retry(
                            payload,
                            message_id=message_id,
                            correlation_id=correlation_id,
                            attempt=attempt + 1,
                        )
                    await message.ack()
                except Exception:
                    await message.nack(requeue=True)

    async def _mark_terminal_failure(self, run_id: uuid.UUID, error_type: str) -> None:
        conversation_id: uuid.UUID | None = None
        async with self.database.session_factory() as session:
            run = await session.get(AgentRun, run_id, with_for_update=True)
            if run is not None and run.status != "completed":
                run.status = "failed"
                run.error_type = error_type
                run.completed_at = datetime.now(UTC)
                conversation_id = run.conversation_id
                conversation = await session.get(Conversation, run.conversation_id)
                if conversation is not None:
                    conversation.status = "active"
                await session.commit()
        if conversation_id is not None:
            try:
                await self.broker.publish(
                    str(conversation_id),
                    "assistant.failed",
                    {"run_id": str(run_id), "error": error_type},
                )
            except Exception:
                logger.exception(
                    "agent_reply_terminal_failure_event_failed",
                    run_id=str(run_id),
                )


async def run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    database = Database(settings.database_url)
    circuits = RedisCircuitRegistry(
        settings.redis_url, threshold=settings.circuit_failure_threshold,
        window_seconds=settings.circuit_window_seconds,
        open_seconds=settings.circuit_open_seconds,
    )
    broker = create_event_broker(settings.event_backend, settings.redis_url)
    memory = create_conversation_memory(
        settings.event_backend,
        settings.redis_url,
        max_messages=settings.agent_history_messages,
        ttl_seconds=settings.conversation_memory_ttl_seconds,
    )
    storage = ObjectStorage(settings)
    knowledge = KnowledgeService(
        database=database,
        storage=storage,
        embeddings=create_embedding_provider(settings, circuits.breaker("embedding")),
        settings=settings,
        reranker=create_reranker(settings),
    )
    order_gateway = create_order_gateway(settings, database, circuits.breaker("oms"))
    customer_operations = CustomerOperationService(
        database,
        order_gateway,
        MockOmsAdapter(database),
        MockInvoiceAdapter(),
    )
    responder = SupervisorAgent(
        settings,
        database,
        knowledge,
        OrderService(order_gateway),
        TicketService(database),
        AfterSalesAgent(settings, circuits.breaker("model")),
        AfterSalesCaseService(database, order_gateway, storage, settings),
        customer_operations=customer_operations,
        model_breaker=circuits.breaker("model"),
    )
    leases = ConversationLeaseManager(
        settings.redis_url,
        ttl_seconds=settings.rabbitmq_conversation_lock_ttl_seconds,
    )
    command_bus = RabbitCommandBus(settings)
    await command_bus.connect()
    if settings.minio_enabled:
        await storage.ensure_bucket()
    if settings.embedding_warmup_on_startup:
        await knowledge.warmup()

    consumer = AgentReplyConsumer(
        settings=settings,
        database=database,
        broker=broker,
        memory=memory,
        responder=responder,
        command_bus=command_bus,
        leases=leases,
    )
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:
            pass

    logger.info(
        "agent_reply_worker_started",
        queue=settings.rabbitmq_agent_queue,
        prefetch=settings.rabbitmq_prefetch_count,
    )
    try:
        async for message in command_bus.messages():
            if stopping.is_set():
                await message.nack(requeue=True)
                break
            await consumer.handle(message)
    finally:
        await command_bus.close()
        await circuits.close()
        await leases.close()
        await memory.close()
        await broker.close()
        await knowledge.close()
        await database.dispose()
        logger.info("agent_reply_worker_stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
