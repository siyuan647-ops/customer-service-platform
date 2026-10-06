from __future__ import annotations

import time
import uuid
import re
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from backend.app.api.auth import router as auth_router
from backend.app.api.conversations import router as conversations_router
from backend.app.api.after_sales import router as after_sales_router
from backend.app.api.after_sales_admin import router as after_sales_admin_router
from backend.app.api.health import router as health_router
from backend.app.api.knowledge import router as knowledge_router
from backend.app.api.customer_operations import router as customer_operations_router
from backend.app.config import Settings, get_settings
from backend.app.conversation_memory import create_conversation_memory
from backend.app.database import Database
from backend.app.events import create_event_broker
from backend.app.knowledge.embeddings import create_embedding_provider
from backend.app.knowledge.reranker import create_reranker
from backend.app.knowledge.service import KnowledgeService
from backend.app.logging import configure_logging
from backend.app.agents.after_sales import AfterSalesAgent
from backend.app.agents.supervisor import SupervisorAgent
from backend.app.orders import create_order_gateway
from backend.app.integrations import MockInvoiceAdapter, MockOmsAdapter, MockPaymentAdapter
from backend.app.services.after_sales_execution import AfterSalesExecutionService
from backend.app.services.order import OrderService
from backend.app.services.after_sales_cases import AfterSalesCaseService
from backend.app.services.tickets import TicketService
from backend.app.services.customer_operations import CustomerOperationService
from backend.app.storage import ObjectStorage
from backend.app.security.sessions import MemorySessionStore, RedisSessionStore, require_customer
from backend.app.security.rate_limit import MemoryDualRateLimiter, RedisDualRateLimiter, resolve_client_ip
from backend.app.security.circuit_breaker import (
    CircuitOpenError, MemoryCircuitRegistry, RedisCircuitRegistry,
)


_MESSAGE_PATH = re.compile(r"^/conversations/[^/]+/messages$")
_EVIDENCE_PATH = re.compile(r"^/after-sales/cases/[^/]+/evidence$")


def create_app(settings_override: Settings | None = None) -> FastAPI:
    settings = settings_override or get_settings()
    if settings.test_identity_header_enabled and settings.app_env != "test":
        raise ValueError("Test identity headers are only allowed in the test environment")
    configure_logging(settings.log_level)
    logger = structlog.get_logger()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.database = Database(settings.database_url)
        if settings.app_env == "test" and settings.event_backend == "memory":
            app.state.customer_sessions = MemorySessionStore(settings.session_ttl_seconds)
            app.state.rate_limiter = MemoryDualRateLimiter()
            app.state.circuits = MemoryCircuitRegistry(
                threshold=settings.circuit_failure_threshold,
                window_seconds=settings.circuit_window_seconds,
                open_seconds=settings.circuit_open_seconds,
            )
        else:
            app.state.customer_sessions = RedisSessionStore(
                settings.redis_url, settings.session_ttl_seconds
            )
            app.state.rate_limiter = RedisDualRateLimiter(settings.redis_url)
            app.state.circuits = RedisCircuitRegistry(
                settings.redis_url,
                threshold=settings.circuit_failure_threshold,
                window_seconds=settings.circuit_window_seconds,
                open_seconds=settings.circuit_open_seconds,
            )
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
            embeddings=create_embedding_provider(settings, app.state.circuits.breaker("embedding")),
            settings=settings,
            reranker=create_reranker(settings),
        )
        app.state.order_gateway = create_order_gateway(
            settings, app.state.database, app.state.circuits.breaker("oms")
        )
        app.state.orders = OrderService(app.state.order_gateway)
        app.state.after_sales_cases = AfterSalesCaseService(
            app.state.database,
            app.state.order_gateway,
            app.state.storage,
            settings,
        )
        app.state.customer_operations = CustomerOperationService(
            app.state.database,
            app.state.order_gateway,
            MockOmsAdapter(app.state.database),
            MockInvoiceAdapter(),
        )
        app.state.after_sales_execution = AfterSalesExecutionService(
            app.state.database,
            app.state.order_gateway,
            MockOmsAdapter(app.state.database),
            MockPaymentAdapter(app.state.database),
            settings,
        )
        app.state.tickets = TicketService(app.state.database)
        app.state.after_sales = AfterSalesAgent(settings, app.state.circuits.breaker("model"))
        app.state.responder = SupervisorAgent(
            settings,
            app.state.database,
            app.state.knowledge,
            app.state.orders,
            app.state.tickets,
            app.state.after_sales,
            app.state.after_sales_cases,
            customer_operations=app.state.customer_operations,
            model_breaker=app.state.circuits.breaker("model"),
        )
        if settings.auto_create_schema:
            await app.state.database.create_schema()
        if settings.minio_enabled:
            await app.state.storage.ensure_bucket()
        if settings.embedding_warmup_on_startup:
            await app.state.knowledge.warmup()
        logger.info("application_started", environment=settings.app_env)
        yield
        await app.state.circuits.close()
        await app.state.rate_limiter.close()
        await app.state.customer_sessions.close()
        await app.state.conversation_memory.close()
        await app.state.broker.close()
        await app.state.knowledge.close()
        await app.state.database.dispose()
        logger.info("application_stopped")

    application = FastAPI(title=settings.app_name, version="0.4.0", lifespan=lifespan)

    @application.exception_handler(CircuitOpenError)
    async def circuit_unavailable(_request: Request, exc: CircuitOpenError):
        return JSONResponse(
            {"detail": f"{exc.dependency} is temporarily unavailable"},
            status_code=503, headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.middleware("http")
    async def customer_rate_limit(request: Request, call_next):
        origin = request.headers.get("origin")
        same_origin = f"{request.url.scheme}://{request.url.netloc}"
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and origin:
            if origin != same_origin and origin not in settings.api_cors_origins:
                return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        path = request.url.path.removeprefix("/api/v1")
        if request.method == "POST" and _MESSAGE_PATH.fullmatch(path):
            scope, window, user_limit, ip_limit = (
                "message", 60, settings.rate_limit_message_user, settings.rate_limit_message_ip
            )
        elif request.method == "POST" and _EVIDENCE_PATH.fullmatch(path):
            scope, window, user_limit, ip_limit = (
                "evidence", 600, settings.rate_limit_evidence_user, settings.rate_limit_evidence_ip
            )
        else:
            return await call_next(request)
        try:
            customer_id = await require_customer(request)
            client_ip = resolve_client_ip(
                request.client.host if request.client else "unknown",
                request.headers.get("X-Forwarded-For"), settings.trusted_proxy_cidrs,
            )
            retry_after = await request.app.state.rate_limiter.check(
                scope, customer_id, client_ip,
                window_seconds=window, user_limit=user_limit, ip_limit=ip_limit,
            )
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        except RedisError:
            return JSONResponse({"detail": "Request protection unavailable"}, status_code=503)
        if retry_after:
            return JSONResponse(
                {"detail": "Too many requests"}, status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)

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
    application.include_router(auth_router)
    application.include_router(knowledge_router)
    # Keep the requested unversioned contract, while retaining /api/v1 as a
    # compatibility alias for clients that already use it.
    application.include_router(conversations_router)
    application.include_router(after_sales_router)
    application.include_router(after_sales_admin_router)
    application.include_router(customer_operations_router)
    application.include_router(
        conversations_router, prefix="/api/v1", include_in_schema=False
    )
    application.include_router(auth_router, prefix="/api/v1", include_in_schema=False)
    application.include_router(knowledge_router, prefix="/api/v1", include_in_schema=False)
    application.include_router(after_sales_router, prefix="/api/v1", include_in_schema=False)
    application.include_router(
        after_sales_admin_router, prefix="/api/v1", include_in_schema=False
    )
    application.include_router(
        customer_operations_router, prefix="/api/v1", include_in_schema=False
    )
    return application


app = create_app()
