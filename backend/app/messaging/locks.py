from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from redis.asyncio import Redis


_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class ConversationLeaseManager:
    """Short Redis lease preventing concurrent replies in one conversation."""

    def __init__(self, redis_url: str, *, ttl_seconds: int) -> None:
        self.redis = Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _key(conversation_id: str) -> str:
        return f"conversation:{conversation_id}:agent-lock"

    @asynccontextmanager
    async def acquire(self, conversation_id: str) -> AsyncIterator[bool]:
        key = self._key(conversation_id)
        token = str(uuid.uuid4())
        acquired = bool(
            await self.redis.set(key, token, ex=self.ttl_seconds, nx=True)
        )
        if not acquired:
            yield False
            return

        stop_renewal = asyncio.Event()
        renewal = asyncio.create_task(self._renew(key, token, stop_renewal))
        try:
            yield True
        finally:
            stop_renewal.set()
            await renewal
            await self.redis.eval(_RELEASE_SCRIPT, 1, key, token)

    async def _renew(self, key: str, token: str, stopping: asyncio.Event) -> None:
        interval = max(1.0, self.ttl_seconds / 3)
        while not stopping.is_set():
            try:
                await asyncio.wait_for(stopping.wait(), timeout=interval)
            except TimeoutError:
                renewed = await self.redis.eval(
                    _RENEW_SCRIPT,
                    1,
                    key,
                    token,
                    self.ttl_seconds,
                )
                if not renewed:
                    return

    async def close(self) -> None:
        await self.redis.aclose()

