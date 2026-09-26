from __future__ import annotations

import asyncio
import signal

import structlog

from backend.app.config import get_settings
from backend.app.database import Database
from backend.app.events import create_event_broker
from backend.app.logging import configure_logging
from backend.app.messaging.outbox import AgentReplyOutboxRelay
from backend.app.messaging.rabbitmq import RabbitCommandBus


async def run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger()
    database = Database(settings.database_url)
    broker = create_event_broker(settings.event_backend, settings.redis_url)
    command_bus = RabbitCommandBus(settings)
    await command_bus.connect()
    relay = AgentReplyOutboxRelay(
        database,
        command_bus,
        max_attempts=settings.rabbitmq_publish_max_attempts,
        lock_timeout_seconds=settings.rabbitmq_publish_lock_timeout_seconds,
        failure_broker=broker,
    )
    recovered = await relay.recover_stale_claims()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:
            pass

    logger.info("agent_reply_outbox_relay_started", recovered_stale_events=recovered)
    try:
        while not stopping.is_set():
            processed = await relay.process_once()
            if not processed:
                try:
                    await asyncio.wait_for(
                        stopping.wait(),
                        timeout=settings.rabbitmq_publish_poll_seconds,
                    )
                except TimeoutError:
                    pass
    finally:
        await command_bus.close()
        await broker.close()
        await database.dispose()
        logger.info("agent_reply_outbox_relay_stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()

