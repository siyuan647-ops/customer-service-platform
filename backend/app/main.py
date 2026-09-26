from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from backend.app.api.conversations import router as conversations_router
from backend.app.api.after_sales import router as after_sales_router
from backend.app.api.after_sales_admin import router as after_sales_admin_router
from backend.app.api.health import router as health_router
from backend.app.api.knowledge import router as knowledge_router
from backend.app.config import Settings, get_settings
from backend.app.conversation_memory import create_conversation_memory
from backend.app.database import Database
from backend.app.events import create_event_broker
from backend.app.knowledge.embeddings import create_embedding_provider
from backend.app.knowledge.service import KnowledgeService
from backend.app.logging import configure_logging
from backend.app.agents.after_sales import AfterSalesAgent
from backend.app.agents.supervisor import SupervisorAgent
from backend.app.orders import create_order_gateway
from backend.app.integrations import MockOmsAdapter, MockPaymentAdapter
from backend.app.services.after_sales_execution import AfterSalesExecutionService
from backend.app.services.order import OrderService
from backend.app.services.after_sales_cases import AfterSalesCaseService
from backend.app.services.tickets import TicketService
from backend.app.storage import ObjectStorage


def create_app(settings_override: Settings | None = None) -> FastAPI:
    settings = settings_override or get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.database = Database(settings.database_url)
        app.state.broker = create_event_broker(settings.event_backend, settings.redis_url)
        app.state.conversation_memory = create_conversation_memory(
            settings.event_backend,
            settings.redis_url,
            max_messages=settings.agent_history_messages,
            ttl_seconds=settings.conversation_memory_ttl_seconds,
        )
        app.state.storage = ObjectStorage(settings)
        app.state.knowledge = KnowledgeService(
            database=app.state.database,
            storage=app.state.storage,
            embeddings=create_embedding_provider(settings),
            settings=settings,
        )
        app.state.order_gateway = create_order_gateway(settings, app.state.database)
        app.state.orders = OrderService(app.state.order_gateway)
        app.state.after_sales_cases = AfterSalesCaseService(
            app.state.database,
            app.state.order_gateway,
            app.state.storage,
            settings,
        )
        app.state.after_sales_execution = AfterSalesExecutionService(
            app.state.database,
            app.state.order_gateway,
            MockOmsAdapter(app.state.database),
            MockPaymentAdapter(app.state.database),
            settings,
        )
        app.state.tickets = TicketService(app.state.database)
        app.state.after_sales = AfterSalesAgent(settings)
        app.state.responder = SupervisorAgent(
            settings,
            app.state.database,
            app.state.knowledge,
            app.state.orders,
            app.state.tickets,
            app.state.after_sales,
            app.state.after_sales_cases,
        )
        if settings.auto_create_schema:
            await app.state.database.create_schema()
        if settings.minio_enabled:
            await app.state.storage.ensure_bucket()
        if settings.embedding_warmup_on_startup:
            await app.state.knowledge.warmup()
        logger.info("application_started", environment=settings.app_env)
        yield
        await app.state.conversation_memory.close()
        await app.state.broker.close()
        await app.state.knowledge.close()
        await app.state.database.dispose()
        logger.info("application_stopped")

    application = FastAPI(title=settings.app_name, version="0.4.0", lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.middleware("http")
    async def request_logging(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        started = time.perf_counter()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "http_request",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            return response
        finally:
            structlog.contextvars.clear_contextvars()

    application.include_router(health_router)
    application.include_router(knowledge_router)
    # Keep the requested unversioned contract, while retaining /api/v1 as a
    # compatibility alias for clients that already use it.
    application.include_router(conversations_router)
    application.include_router(after_sales_router)
    application.include_router(after_sales_admin_router)
    application.include_router(
        conversations_router, prefix="/api/v1", include_in_schema=False
    )
    application.include_router(knowledge_router, prefix="/api/v1", include_in_schema=False)
    application.include_router(after_sales_router, prefix="/api/v1", include_in_schema=False)
    application.include_router(
        after_sales_admin_router, prefix="/api/v1", include_in_schema=False
    )
    return application


app = create_app()
