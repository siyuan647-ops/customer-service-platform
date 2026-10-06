from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import math
import time
import uuid
from collections import defaultdict

from redis.asyncio import Redis


_DUAL_WINDOW_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now - window)
local user_count = redis.call('ZCARD', KEYS[1])
local ip_count = redis.call('ZCARD', KEYS[2])
if user_count >= tonumber(ARGV[3]) or ip_count >= tonumber(ARGV[4]) then
  local retry = 0
  if user_count >= tonumber(ARGV[3]) then
    local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    retry = math.max(retry, tonumber(oldest[2]) + window - now)
  end
  if ip_count >= tonumber(ARGV[4]) then
    local oldest = redis.call('ZRANGE', KEYS[2], 0, 0, 'WITHSCORES')
    retry = math.max(retry, tonumber(oldest[2]) + window - now)
  end
  return {0, retry}
end
redis.call('ZADD', KEYS[1], now, ARGV[5])
redis.call('ZADD', KEYS[2], now, ARGV[5])
redis.call('PEXPIRE', KEYS[1], window)
redis.call('PEXPIRE', KEYS[2], window)
return {1, 0}
"""


def resolve_client_ip(peer: str, forwarded_for: str | None, trusted_proxies: list[str]) -> str:
    """Use forwarded addresses only when every nearer hop is a trusted proxy."""
    try:
        peer_address = ipaddress.ip_address(peer)
        networks = [ipaddress.ip_network(value, strict=False) for value in trusted_proxies]
    except ValueError:
        return peer
    if not forwarded_for or not any(peer_address in network for network in networks):
        return peer
    try:
        hops = [ipaddress.ip_address(value.strip()) for value in forwarded_for.split(",")]
    except ValueError:
        return peer
    if not hops:
        return peer
    for address in reversed(hops):
        if not any(address in network for network in networks):
            return str(address)
    return str(hops[0])


def _keys(scope: str, customer_id: uuid.UUID, ip: str) -> tuple[str, str]:
    ip_digest = hashlib.sha256(ip.encode()).hexdigest()[:32]
    return (f"rate:{scope}:customer:{customer_id}", f"rate:{scope}:ip:{ip_digest}")


class RedisDualRateLimiter:
    def __init__(self, redis_url: str) -> None:
        self.redis = Redis.from_url(redis_url, decode_responses=True)

    async def check(
        self, scope: str, customer_id: uuid.UUID, ip: str, *,
        window_seconds: int, user_limit: int, ip_limit: int,
    ) -> int:
        keys = _keys(scope, customer_id, ip)
        accepted, retry_ms = await self.redis.eval(
            _DUAL_WINDOW_SCRIPT, 2, *keys, int(time.time() * 1000),
            window_seconds * 1000, user_limit, ip_limit, uuid.uuid4().hex,
        )
        return 0 if accepted else max(1, math.ceil(retry_ms / 1000))

    async def close(self) -> None:
        await self.redis.aclose()


class MemoryDualRateLimiter:
    """Test-only equivalent of the Redis atomic dual-window policy."""

    def __init__(self) -> None:
        self.values: dict[str, list[float]] = defaultdict(list)
        self.lock = asyncio.Lock()

    async def check(
        self, scope: str, customer_id: uuid.UUID, ip: str, *,
        window_seconds: int, user_limit: int, ip_limit: int,
    ) -> int:
        keys = _keys(scope, customer_id, ip)
        now = time.monotonic()
        async with self.lock:
            for key in keys:
                self.values[key] = [value for value in self.values[key] if value > now - window_seconds]
            retries = [
                self.values[key][0] + window_seconds - now
                for key, limit in zip(keys, (user_limit, ip_limit))
                if len(self.values[key]) >= limit
            ]
            if retries:
                return max(1, math.ceil(max(retries)))
            for key in keys:
                self.values[key].append(now)
            return 0

    async def close(self) -> None:
        self.values.clear()
