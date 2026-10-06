from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError
from redis.asyncio import Redis


_BEFORE_SCRIPT = """
local until_ms = tonumber(redis.call('HGET', KEYS[1], 'open_until') or '0')
local now = tonumber(ARGV[1])
if until_ms > now then return {0, until_ms - now, 0} end
if until_ms > 0 then
  if not redis.call('SET', KEYS[2], ARGV[2], 'NX', 'PX', ARGV[3]) then
    return {0, 1000, 0}
  end
  return {1, 0, 1}
end
return {1, 0, 0}
"""

_FAIL_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local open_ms = tonumber(ARGV[3])
if ARGV[6] == '1' then
  if redis.call('GET', KEYS[3]) ~= ARGV[5] then return 0 end
  redis.call('DEL', KEYS[3], KEYS[2])
  redis.call('HSET', KEYS[1], 'open_until', now + open_ms)
  redis.call('PEXPIRE', KEYS[1], open_ms + window + 60000)
  return 1
end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now - window)
redis.call('ZADD', KEYS[2], now, ARGV[4])
redis.call('PEXPIRE', KEYS[2], window)
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[7]) then
  redis.call('HSET', KEYS[1], 'open_until', now + open_ms)
  redis.call('PEXPIRE', KEYS[1], open_ms + window + 60000)
  redis.call('DEL', KEYS[2])
end
return 1
"""

_SUCCESS_SCRIPT = """
if redis.call('GET', KEYS[3]) == ARGV[1] then
  redis.call('DEL', KEYS[1], KEYS[2], KEYS[3])
  return 1
end
return 0
"""


class CircuitOpenError(RuntimeError):
    def __init__(self, dependency: str, retry_after_seconds: int) -> None:
        self.dependency = dependency
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"{dependency} is temporarily unavailable")


def is_transient_failure(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, httpx.TransportError, APIConnectionError, APITimeoutError)):
            return True
        if isinstance(current, httpx.HTTPStatusError) and current.response.status_code >= 500:
            return True
        if isinstance(current, APIStatusError) and current.status_code >= 500:
            return True
        current = current.__cause__ or current.__context__
    return False


class CircuitBreaker:
    def __init__(self, registry, name: str) -> None:
        self.registry = registry
        self.name = name

    @asynccontextmanager
    async def guard(self) -> AsyncIterator[None]:
        token = uuid.uuid4().hex
        allowed, retry_after, probe = await self.registry.before(self.name, token)
        if not allowed:
            raise CircuitOpenError(self.name, retry_after)
        try:
            yield
        except BaseException as exc:
            if is_transient_failure(exc) or isinstance(exc, asyncio.CancelledError):
                await self.registry.failure(self.name, token, probe)
            elif probe:
                # A 4xx or validation error proves the remote dependency responds.
                await self.registry.success(self.name, token, probe)
            raise
        else:
            await self.registry.success(self.name, token, probe)


@asynccontextmanager
async def optional_guard(breaker: CircuitBreaker | None) -> AsyncIterator[None]:
    if breaker is None:
        yield
    else:
        async with breaker.guard():
            yield


class RedisCircuitRegistry:
    def __init__(self, redis_url: str, *, threshold: int, window_seconds: int, open_seconds: int) -> None:
        self.redis = Redis.from_url(redis_url, decode_responses=True)
        self.threshold = threshold
        self.window_ms = window_seconds * 1000
        self.open_ms = open_seconds * 1000

    def breaker(self, name: str) -> CircuitBreaker:
        return CircuitBreaker(self, name)

    @staticmethod
    def _keys(name: str) -> tuple[str, str, str]:
        return f"circuit:{name}:state", f"circuit:{name}:failures", f"circuit:{name}:probe"

    async def before(self, name: str, token: str) -> tuple[bool, int, bool]:
        state, _, probe = self._keys(name)
        allowed, remaining_ms, half_open = await self.redis.eval(
            _BEFORE_SCRIPT, 2, state, probe, int(time.time() * 1000), token,
            max(self.open_ms, 120_000),
        )
        return bool(allowed), max(1, (remaining_ms + 999) // 1000), bool(half_open)

    async def failure(self, name: str, token: str, probe: bool) -> None:
        await self.redis.eval(
            _FAIL_SCRIPT, 3, *self._keys(name), int(time.time() * 1000),
            self.window_ms, self.open_ms, uuid.uuid4().hex, token,
            int(probe), self.threshold,
        )

    async def success(self, name: str, token: str, probe: bool) -> None:
        if probe:
            await self.redis.eval(_SUCCESS_SCRIPT, 3, *self._keys(name), token)

    async def close(self) -> None:
        await self.redis.aclose()


class MemoryCircuitRegistry:
    """Test-only circuit registry with the same states as the Redis implementation."""

    def __init__(self, *, threshold: int, window_seconds: int, open_seconds: int) -> None:
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.open_seconds = open_seconds
        self.failures: dict[str, list[float]] = {}
        self.open_until: dict[str, float] = {}
        self.probes: dict[str, str] = {}
        self.lock = asyncio.Lock()

    def breaker(self, name: str) -> CircuitBreaker:
        return CircuitBreaker(self, name)

    async def before(self, name: str, token: str) -> tuple[bool, int, bool]:
        async with self.lock:
            now = time.monotonic()
            until = self.open_until.get(name, 0)
            if until > now:
                return False, max(1, int(until - now + 0.999)), False
            if until:
                if name in self.probes:
                    return False, 1, False
                self.probes[name] = token
                return True, 0, True
            return True, 0, False

    async def failure(self, name: str, token: str, probe: bool) -> None:
        async with self.lock:
            if probe:
                if self.probes.get(name) != token:
                    return
                self.probes.pop(name, None)
                self.open_until[name] = time.monotonic() + self.open_seconds
                self.failures.pop(name, None)
                return
            now = time.monotonic()
            values = [value for value in self.failures.get(name, []) if value > now - self.window_seconds]
            values.append(now)
            if len(values) >= self.threshold:
                self.open_until[name] = now + self.open_seconds
                self.failures.pop(name, None)
            else:
                self.failures[name] = values

    async def success(self, name: str, token: str, probe: bool) -> None:
        if probe:
            async with self.lock:
                if self.probes.get(name) == token:
                    self.probes.pop(name, None)
                    self.open_until.pop(name, None)
                    self.failures.pop(name, None)

    async def close(self) -> None:
        self.failures.clear()
        self.open_until.clear()
        self.probes.clear()
