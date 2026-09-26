from __future__ import annotations

import asyncio
import signal

import structlog

from backend.app.agents.evidence import EvidenceAnalyzer
from backend.app.config import get_settings
from backend.app.database import Database
from backend.app.integrations import MockOmsAdapter, MockPaymentAdapter
from backend.app.logging import configure_logging
from backend.app.orders import create_order_gateway
from backend.app.services.after_sales_execution import AfterSalesExecutionService
from backend.app.services.evidence_analysis import EvidenceAnalysisService
from backend.app.storage import ObjectStorage


async def run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger()
    database = Database(settings.database_url)
    orders = create_order_gateway(settings, database)
    execution_service = AfterSalesExecutionService(
        database,
        orders,
        MockOmsAdapter(database),
        MockPaymentAdapter(database),
        settings,
    )
    storage = ObjectStorage(settings)
    evidence_service = EvidenceAnalysisService(
        database,
        storage,
        orders,
        EvidenceAnalyzer(settings),
        settings,
    )
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:
            pass

    if settings.evidence_analysis_enabled:
        await storage.ensure_bucket()
    logger.info(
        "after_sales_worker_started",
        evidence_analysis_enabled=settings.evidence_analysis_enabled,
    )
    try:
        while not stopping.is_set():
            execution_processed = await execution_service.process_once()
            evidence_processed = False
            if settings.evidence_analysis_enabled:
                evidence_processed = await evidence_service.process_once()
            if not execution_processed and not evidence_processed:
                try:
                    await asyncio.wait_for(
                        stopping.wait(),
                        timeout=settings.after_sales_worker_poll_seconds,
                    )
                except TimeoutError:
                    pass
    finally:
        await database.dispose()
        logger.info("after_sales_worker_stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
