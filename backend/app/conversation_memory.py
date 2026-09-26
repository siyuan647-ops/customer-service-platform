from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from typing import Protocol

from redis.asyncio import Redis

from backend.app.agents.contracts import ConversationTurn


class ConversationMemory(Protocol):
    async def load(self, conversation_id: str) -> list[ConversationTurn]: ...
    async def replace(
        self, conversation_id: str, turns: list[ConversationTurn]
    ) -> None: ...
    async def append(self, conversation_id: str, turn: ConversationTurn) -> None: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


class RedisConversationMemory:
    """Sliding-TTL short-term context backed by one Redis list per conversation."""

    def __init__(self, url: str, *, max_messages: int, ttl_seconds: int) -> None:
        self.redis = Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=False,
        )
        self.max_messages = max_messages
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _key(conversation_id: str) -> str:
        return f"conversation:{conversation_id}:short-term-context"

    async def load(self, conversation_id: str) -> list[ConversationTurn]:
        key = self._key(conversation_id)
        values = await self.redis.lrange(key, -self.max_messages, -1)
        if not values:
            return []
        # Reading the context is conversation activity, so refresh the sliding TTL.
        await self.redis.expire(key, self.ttl_seconds)
        return [ConversationTurn.model_validate(json.loads(value)) for value in values]

    async def append(self, conversation_id: str, turn: ConversationTurn) -> None:
        key = self._key(conversation_id)
        value = json.dumps(turn.model_dump(mode="json"), ensure_ascii=False)
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.rpush(key, value)
            pipeline.ltrim(key, -self.max_messages, -1)
            pipeline.expire(key, self.ttl_seconds)
            await pipeline.execute()

    async def replace(
        self, conversation_id: str, turns: list[ConversationTurn]
    ) -> None:
        key = self._key(conversation_id)
        values = [
            json.dumps(turn.model_dump(mode="json"), ensure_ascii=False)
            for turn in turns[-self.max_messages :]
        ]
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.delete(key)
            if values:
                pipeline.rpush(key, *values)
                pipeline.expire(key, self.ttl_seconds)
            await pipeline.execute()

    async def ping(self) -> bool:
        return bool(await self.redis.ping())

    async def close(self) -> None:
        await self.redis.aclose()


class MemoryConversationMemory:
    """TTL-aware in-process implementation used by tests."""

    def __init__(self, *, max_messages: int, ttl_seconds: int) -> None:
        self.max_messages = max_messages
        self.ttl_seconds = ttl_seconds
        self._turns: dict[str, list[ConversationTurn]] = defaultdict(list)
        self._expires_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _is_expired(self, conversation_id: str, now: float) -> bool:
        expires_at = self._expires_at.get(conversation_id)
        return expires_at is not None and expires_at <= now

    async def load(self, conversation_id: str) -> list[ConversationTurn]:
        async with self._lock:
            now = time.monotonic()
            if self._is_expired(conversation_id, now):
                self._turns.pop(conversation_id, None)
                self._expires_at.pop(conversation_id, None)
                return []
            turns = self._turns.get(conversation_id)
            if not turns:
                return []
            self._expires_at[conversation_id] = now + self.ttl_seconds
            return list(turns[-self.max_messages :])

    async def append(self, conversation_id: str, turn: ConversationTurn) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._is_expired(conversation_id, now):
                self._turns.pop(conversation_id, None)
            turns = self._turns[conversation_id]
            turns.append(turn)
            del turns[: -self.max_messages]
            self._expires_at[conversation_id] = now + self.ttl_seconds

    async def replace(
        self, conversation_id: str, turns: list[ConversationTurn]
    ) -> None:
        async with self._lock:
            if not turns:
                self._turns.pop(conversation_id, None)
                self._expires_at.pop(conversation_id, None)
                return
            self._turns[conversation_id] = list(turns[-self.max_messages :])
            self._expires_at[conversation_id] = time.monotonic() + self.ttl_seconds

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._turns.clear()
        self._expires_at.clear()


def create_conversation_memory(
    backend: str,
    redis_url: str,
    *,
    max_messages: int,
    ttl_seconds: int,
) -> ConversationMemory:
    if backend == "memory":
        return MemoryConversationMemory(
            max_messages=max_messages,
            ttl_seconds=ttl_seconds,
        )
    return RedisConversationMemory(
        redis_url,
        max_messages=max_messages,
        ttl_seconds=ttl_seconds,
    )
