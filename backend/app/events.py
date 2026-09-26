from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError


@dataclass(slots=True)
class StreamEvent:
    id: str
    event: str
    data: dict[str, Any]


class EventBroker(Protocol):
    async def publish(self, conversation_id: str, event: str, data: dict[str, Any]) -> str: ...
    async def read(
        self, conversation_id: str, last_id: str, block_ms: int = 15_000
    ) -> list[StreamEvent]: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


class RedisEventBroker:
    def __init__(self, url: str) -> None:
        # redis-py 8 defaults socket reads to 5 seconds, while XREAD blocks for
        # 15 seconds. Keep the socket timeout above the blocking window so an
        # idle SSE connection is not mistaken for a failed Redis connection.
        self.redis = Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=20,
            retry_on_timeout=False,
        )

    @staticmethod
    def _key(conversation_id: str) -> str:
        return f"conversation:{conversation_id}:events"

    async def publish(self, conversation_id: str, event: str, data: dict[str, Any]) -> str:
        return await self.redis.xadd(
            self._key(conversation_id),
            {"event": event, "data": json.dumps(data, ensure_ascii=False)},
            maxlen=1000,
            approximate=True,
        )

    async def read(
        self, conversation_id: str, last_id: str, block_ms: int = 15_000
    ) -> list[StreamEvent]:
        try:
            result = await self.redis.xread(
                {self._key(conversation_id): last_id}, count=100, block=block_ms
            )
        except RedisTimeoutError:
            # A transient/idle read timeout should keep SSE alive; the next
            # generator iteration retries and EventSourceResponse sends pings.
            return []
        events: list[StreamEvent] = []
        for _stream, entries in result:
            for event_id, fields in entries:
                events.append(
                    StreamEvent(
                        id=event_id,
                        event=fields["event"],
                        data=json.loads(fields["data"]),
                    )
                )
        return events

    async def ping(self) -> bool:
        return bool(await self.redis.ping())

    async def close(self) -> None:
        await self.redis.aclose()


class MemoryEventBroker:
    """Deterministic event broker for unit tests and dependency-free development."""

    def __init__(self) -> None:
        self._events: dict[str, list[StreamEvent]] = defaultdict(list)
        self._conditions: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)

    async def publish(self, conversation_id: str, event: str, data: dict[str, Any]) -> str:
        condition = self._conditions[conversation_id]
        async with condition:
            event_id = f"{len(self._events[conversation_id]) + 1}-0"
            self._events[conversation_id].append(StreamEvent(event_id, event, data))
            condition.notify_all()
            return event_id

    async def read(
        self, conversation_id: str, last_id: str, block_ms: int = 15_000
    ) -> list[StreamEvent]:
        start = 0 if last_id == "0-0" else int(last_id.split("-", 1)[0])
        existing = self._events[conversation_id][start:]
        if existing or block_ms == 0:
            return list(existing)
        condition = self._conditions[conversation_id]
        try:
            async with asyncio.timeout(block_ms / 1000):
                async with condition:
                    await condition.wait()
        except TimeoutError:
            return []
        return list(self._events[conversation_id][start:])

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def create_event_broker(backend: str, redis_url: str) -> EventBroker:
    return MemoryEventBroker() if backend == "memory" else RedisEventBroker(redis_url)
