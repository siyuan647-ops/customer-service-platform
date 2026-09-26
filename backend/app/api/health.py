from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from backend.app.schemas import HealthRead


router = APIRouter(tags=["health"])


async def _status(check) -> str:
    try:
        await check()
        return "ok"
    except Exception:
        return "unavailable"


async def _redis_status(request: Request) -> bool:
    broker_ok, memory_ok = await asyncio.gather(
        request.app.state.broker.ping(),
        request.app.state.conversation_memory.ping(),
    )
    return bool(broker_ok and memory_ok)


@router.get("/health", response_model=HealthRead)
async def health(request: Request) -> HealthRead:
    database, redis, minio, knowledge = await asyncio.gather(
        _status(request.app.state.database.ping),
        _status(lambda: _redis_status(request)),
        _status(request.app.state.storage.ping),
        _status(request.app.state.knowledge.ping),
    )
    components = {
        "database": database,
        "redis": redis,
        "minio": minio,
        "knowledge": knowledge,
    }
    overall = "ok" if all(value == "ok" for value in components.values()) else "degraded"
    return HealthRead(
        status=overall, service=request.app.state.settings.app_name, components=components
    )
