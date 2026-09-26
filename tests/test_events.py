import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from backend.app.events import MemoryEventBroker, RedisEventBroker


@pytest.mark.asyncio
async def test_memory_event_broker_replays_after_cursor():
    broker = MemoryEventBroker()
    first = await broker.publish("conversation-1", "message", {"n": 1})
    await broker.publish("conversation-1", "message", {"n": 2})
    replay = await broker.read("conversation-1", first, block_ms=0)
    assert len(replay) == 1
    assert replay[0].data == {"n": 2}


@pytest.mark.asyncio
async def test_redis_read_timeout_is_treated_as_empty_poll():
    class TimeoutRedis:
        async def xread(self, *_args, **_kwargs):
            raise RedisTimeoutError("idle stream")

    broker = RedisEventBroker.__new__(RedisEventBroker)
    broker.redis = TimeoutRedis()

    assert await broker.read("conversation-1", "0-0") == []
