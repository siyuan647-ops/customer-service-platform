from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
import uuid

from fastapi import HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy import select

from backend.app.models import CustomerAccount


def normalize_username(username: str) -> str:
    normalized = username.strip().casefold()
    if not (3 <= len(normalized) <= 128):
        raise ValueError("Username must have 3 to 128 characters")
    return normalized


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must have at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt:16384:8:1:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split(":")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(digest, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def _session_key(token: str) -> str:
    return "customer-session:" + hashlib.sha256(token.encode()).hexdigest()


class RedisSessionStore:
    def __init__(self, redis_url: str, ttl_seconds: int) -> None:
        self.redis = Redis.from_url(redis_url, decode_responses=True)
        self.ttl_seconds = ttl_seconds

    async def create(self, customer_id: uuid.UUID, session_version: int = 1) -> str:
        token = secrets.token_urlsafe(32)
        await self.redis.set(
            _session_key(token), f"{customer_id}:{session_version}", ex=self.ttl_seconds
        )
        return token

    async def get(self, token: str) -> tuple[uuid.UUID, int] | None:
        if not token:
            return None
        value = await self.redis.get(_session_key(token))
        try:
            if not value:
                return None
            customer_id, version = value.split(":", 1)
            return uuid.UUID(customer_id), int(version)
        except ValueError:
            return None

    async def delete(self, token: str) -> None:
        if token:
            await self.redis.delete(_session_key(token))

    async def close(self) -> None:
        await self.redis.aclose()


class MemorySessionStore:
    """Test-only implementation; production sessions always use Redis."""

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.values: dict[str, tuple[uuid.UUID, int, float]] = {}
        self.lock = asyncio.Lock()

    async def create(self, customer_id: uuid.UUID, session_version: int = 1) -> str:
        token = secrets.token_urlsafe(32)
        async with self.lock:
            self.values[_session_key(token)] = (
                customer_id, session_version, time.monotonic() + self.ttl_seconds
            )
        return token

    async def get(self, token: str) -> tuple[uuid.UUID, int] | None:
        async with self.lock:
            entry = self.values.get(_session_key(token)) if token else None
            if entry is None or entry[2] <= time.monotonic():
                return None
            return entry[0], entry[1]

    async def delete(self, token: str) -> None:
        async with self.lock:
            self.values.pop(_session_key(token), None)

    async def close(self) -> None:
        self.values.clear()


async def authenticate_customer(database, username: str, password: str) -> tuple[uuid.UUID, int] | None:
    try:
        normalized = normalize_username(username)
    except ValueError:
        return None
    async with database.session_factory() as session:
        account = await session.scalar(
            select(CustomerAccount).where(CustomerAccount.username == normalized)
        )
    if account is None or not account.is_active:
        return None
    return (
        (account.customer_id, account.session_version)
        if verify_password(password, account.password_hash) else None
    )


async def require_customer(request: Request) -> uuid.UUID:
    cached = getattr(request.state, "authenticated_customer_id", None)
    if cached is not None:
        return cached
    settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name, "")
    principal = await request.app.state.customer_sessions.get(token)
    customer_id = principal[0] if principal is not None else None
    if principal is not None:
        async with request.app.state.database.session_factory() as session:
            account = await session.get(CustomerAccount, customer_id)
        if (
            account is None or not account.is_active
            or account.session_version != principal[1]
        ):
            customer_id = None
    # Existing integration tests use a synthetic customer identity. This path is
    # never enabled in development or production.
    if (
        customer_id is None and settings.app_env == "test"
        and settings.test_identity_header_enabled and not token
    ):
        raw = request.headers.get("X-Customer-ID") or request.query_params.get("customer_id")
        try:
            customer_id = uuid.UUID(raw) if raw else None
        except ValueError:
            customer_id = None
    if customer_id is None:
        raise HTTPException(status_code=401, detail="Customer session required")
    request.state.authenticated_customer_id = customer_id
    return customer_id
