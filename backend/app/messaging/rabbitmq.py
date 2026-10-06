from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, IncomingMessage, Message
from aio_pika.abc import AbstractRobustChannel, AbstractRobustConnection, AbstractRobustExchange

from backend.app.config import Settings


AGENT_REPLY_REQUESTED = "agent.reply.requested"  #main router,request agent send message for users
AGENT_REPLY_DEAD = "dead.agent.reply.requested"  #dead router,fails for many times
AGENT_REPLY_RETRY_DELAYS_MS = (5_000, 30_000, 120_000)


class RabbitCommandBus:
    """RabbitMQ topology and publish/consume operations for agent commands."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.connection: AbstractRobustConnection | None = None  #tcp connection | application->RabbitMQ
        self.channel: AbstractRobustChannel | None = None
        self.exchange: AbstractRobustExchange | None = None  #entrace of message
        self.queue: aio_pika.abc.AbstractRobustQueue | None = None

    async def connect(self) -> None:
        if self.connection is not None and not self.connection.is_closed:
            return
        self.connection = await aio_pika.connect_robust(self.settings.rabbitmq_url)
        self.channel = await self.connection.channel(
            publisher_confirms=True,
            on_return_raises=True,
        )
        await self.channel.set_qos(prefetch_count=self.settings.rabbitmq_prefetch_count)
        self.exchange = await self.channel.declare_exchange(
            self.settings.rabbitmq_exchange,
            ExchangeType.TOPIC,
            durable=True,
        )
        self.queue = await self.channel.declare_queue(
            self.settings.rabbitmq_agent_queue,
            durable=True,
            arguments={
                "x-dead-letter-exchange": self.settings.rabbitmq_exchange,
                "x-dead-letter-routing-key": AGENT_REPLY_DEAD,
            },
        )
        await self.queue.bind(self.exchange, routing_key=AGENT_REPLY_REQUESTED)

        dead_letter_queue = await self.channel.declare_queue(
            "customer_service.dlq",
            durable=True,
        )
        await dead_letter_queue.bind(self.exchange, routing_key=AGENT_REPLY_DEAD)

        for index, delay_ms in enumerate(AGENT_REPLY_RETRY_DELAYS_MS, start=1):
            retry_queue = await self.channel.declare_queue(
                f"{self.settings.rabbitmq_agent_queue}.retry.{index}",
                durable=True,
                arguments={
                    "x-message-ttl": delay_ms,
                    "x-dead-letter-exchange": self.settings.rabbitmq_exchange,
                    "x-dead-letter-routing-key": AGENT_REPLY_REQUESTED,
                },
            )
            await retry_queue.bind(
                self.exchange,
                routing_key=self.retry_routing_key(index),
            )

    @staticmethod
    def retry_routing_key(attempt: int) -> str:
        tier = min(max(attempt, 1), len(AGENT_REPLY_RETRY_DELAYS_MS))
        return f"agent.reply.retry.{tier}"

    async def publish(
        self,
        routing_key: str,
        payload: dict[str, Any],
        *,
        message_id: str,
        correlation_id: str | None = None,
        headers: dict[str, Any] | None = None,
    ) -> None:
        if self.exchange is None:
            raise RuntimeError("RabbitMQ command bus is not connected")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        await self.exchange.publish(
            Message(
                body=body,
                content_type="application/json",
                content_encoding="utf-8",
                delivery_mode=DeliveryMode.PERSISTENT,
                message_id=message_id,
                correlation_id=correlation_id,
                headers=headers or {},
            ),
            routing_key=routing_key,
            mandatory=True,
        )

    async def publish_retry(
        self,
        payload: dict[str, Any],
        *,
        message_id: str,
        correlation_id: str,
        attempt: int,
        delay_tier: int | None = None,
    ) -> None:
        # ``attempt`` is the number of the next execution. Attempt 2 is the
        # first retry and therefore uses the first (5-second) delay tier.
        retry_tier = (
            delay_tier if delay_tier is not None
            else min(max(attempt - 1, 1), len(AGENT_REPLY_RETRY_DELAYS_MS))
        )
        await self.publish(
            self.retry_routing_key(retry_tier),
            payload,
            message_id=message_id,
            correlation_id=correlation_id,
            headers={"x-agent-attempt": attempt},
        )

    async def publish_dead_letter(
        self,
        payload: dict[str, Any],
        *,
        message_id: str,
        correlation_id: str | None,
        attempt: int,
        error_type: str,
    ) -> None:
        await self.publish(
            AGENT_REPLY_DEAD,
            payload,
            message_id=message_id,
            correlation_id=correlation_id,
            headers={
                "x-agent-attempt": attempt,
                "x-error-type": error_type,
            },
        )

    async def messages(self) -> AsyncIterator[IncomingMessage]:
        if self.queue is None:
            raise RuntimeError("RabbitMQ command bus is not connected")
        async with self.queue.iterator() as iterator:
            async for message in iterator:
                yield message

    async def close(self) -> None:
        if self.connection is not None and not self.connection.is_closed:
            await self.connection.close()
