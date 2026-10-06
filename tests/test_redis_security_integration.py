"""Opt-in integration test: set REDIS_TEST_URL to an isolated Redis database."""

from __future__ import annotations

import os
import uuid

import pytest

from backend.app.security.circuit_breaker import CircuitOpenError, RedisCircuitRegistry
from backend.app.security.rate_limit import RedisDualRateLimiter, _keys
from backend.app.security.sessions import RedisSessionStore


@pytest.mark.asyncio
async def test_security_state_is_shared_between_instances():
    url = os.getenv("REDIS_TEST_URL")
    if not url:
        pytest.skip("REDIS_TEST_URL is not set")
    identity = uuid.uuid4()
    scope = "test-" + uuid.uuid4().hex
    first = RedisDualRateLimiter(url)
    second = RedisDualRateLimiter(url)
    sessions_a = RedisSessionStore(url, 300)
    sessions_b = RedisSessionStore(url, 300)
    breakers_a = RedisCircuitRegistry(url, threshold=2, window_seconds=30, open_seconds=1)
    breakers_b = RedisCircuitRegistry(url, threshold=2, window_seconds=30, open_seconds=1)
    try:
        token = await sessions_a.create(identity)
        assert await sessions_b.get(token) == (identity, 1)
        await sessions_b.delete(token)
        assert await sessions_a.get(token) is None

        limits = dict(window_seconds=60, user_limit=2, ip_limit=10)
        assert await first.check(scope, identity, "192.0.2.10", **limits) == 0
        assert await second.check(scope, identity, "192.0.2.11", **limits) == 0
        assert await first.check(scope, identity, "192.0.2.12", **limits) > 0

        name = scope + "-model"
        for breaker in (breakers_a.breaker(name), breakers_b.breaker(name)):
            with pytest.raises(TimeoutError):
                async with breaker.guard():
                    raise TimeoutError("provider timed out")
        with pytest.raises(CircuitOpenError):
            async with breakers_a.breaker(name).guard():
                pass
        import asyncio
        await asyncio.sleep(1.1)
        async with breakers_a.breaker(name).guard():
            with pytest.raises(CircuitOpenError):
                async with breakers_b.breaker(name).guard():
                    pass
        async with breakers_b.breaker(name).guard():
            pass
    finally:
        await first.redis.delete(*_keys(scope, identity, "192.0.2.10"))
        await first.redis.delete(*_keys(scope, identity, "192.0.2.11"))
        await first.redis.delete(*_keys(scope, identity, "192.0.2.12"))
        await breakers_a.redis.delete(*breakers_a._keys(scope + "-model"))
        await first.close()
        await second.close()
        await sessions_a.close()
        await sessions_b.close()
        await breakers_a.close()
        await breakers_b.close()
