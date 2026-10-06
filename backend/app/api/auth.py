from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.app.security.sessions import authenticate_customer, require_customer
from backend.app.security.rate_limit import resolve_client_ip


router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


@router.post("/session")
async def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, str]:
    ip = resolve_client_ip(
        request.client.host if request.client else "unknown",
        request.headers.get("X-Forwarded-For"),
        request.app.state.settings.trusted_proxy_cidrs,
    )
    identity = uuid.uuid5(uuid.NAMESPACE_DNS, payload.username.strip().casefold())
    retry_after = await request.app.state.rate_limiter.check(
        "login", identity, ip, window_seconds=300, user_limit=5, ip_limit=20,
    )
    if retry_after:
        raise HTTPException(
            status_code=429, detail="Too many login attempts", headers={"Retry-After": str(retry_after)}
        )
    principal = await authenticate_customer(
        request.app.state.database, payload.username, payload.password
    )
    if principal is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    customer_id, session_version = principal
    settings = request.app.state.settings
    previous = request.cookies.get(settings.session_cookie_name, "")
    if previous:
        await request.app.state.customer_sessions.delete(previous)
    token = await request.app.state.customer_sessions.create(customer_id, session_version)
    response.set_cookie(
        settings.session_cookie_name, token, max_age=settings.session_ttl_seconds,
        httponly=True, secure=settings.app_env == "production", samesite="lax", path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"customer_id": str(customer_id)}


@router.get("/session")
async def current_session(request: Request) -> dict[str, str]:
    customer_id = await require_customer(request)
    return {"customer_id": str(customer_id)}


@router.delete("/session")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    settings = request.app.state.settings
    await request.app.state.customer_sessions.delete(
        request.cookies.get(settings.session_cookie_name, "")
    )
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True}
