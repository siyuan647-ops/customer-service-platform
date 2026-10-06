from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.security.circuit_breaker import CircuitOpenError, MemoryCircuitRegistry
from backend.app.security.provision import provision_account
from backend.app.security.rate_limit import resolve_client_ip
from backend.app.orders.gateway import HttpOmsGateway, OrderGatewayError
from backend.app.workers.agent_reply import AgentReplyConsumer
from backend.app.messaging.rabbitmq import AGENT_REPLY_REQUESTED


def _app(tmp_path, **overrides):
    values = {
        "app_env": "test",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'security.db'}",
        "event_backend": "memory",
        "agent_task_backend": "rabbitmq",
        "minio_enabled": False,
        "agent_mode": "mock",
        "order_backend": "mock",
        "embedding_mode": "hash",
        "auto_create_schema": True,
    }
    values.update(overrides)
    return create_app(Settings(**values))


def _account(client, username: str, customer_id: uuid.UUID) -> None:
    asyncio.run(provision_account(
        client.app.state.database, username, customer_id, "long-test-password-123"
    ))


def _login(client, username: str):
    return client.post(
        "/auth/session", json={"username": username, "password": "long-test-password-123"}
    )


def test_client_ip_only_uses_forwarded_chain_from_trusted_proxy():
    trusted = ["172.20.0.0/16"]
    assert resolve_client_ip("198.51.100.9", "192.0.2.1", trusted) == "198.51.100.9"
    assert resolve_client_ip("172.20.0.10", "192.0.2.1, 198.51.100.9", trusted) == "198.51.100.9"
    assert resolve_client_ip("172.20.0.10", "192.0.2.1, 172.20.0.11", trusted) == "192.0.2.1"
    assert resolve_client_ip("172.20.0.10", "invalid", trusted) == "172.20.0.10"


def test_session_identity_ignores_spoofed_customer_header_and_can_be_revoked(tmp_path):
    owner = uuid.uuid4()
    other = uuid.uuid4()
    conversation = uuid.uuid4()
    with TestClient(_app(tmp_path)) as client:
        _account(client, "owner", owner)
        _account(client, "other", other)
        # The test-only header bypass is disabled by default.
        unauthenticated = client.post(
            f"/conversations/{conversation}/messages",
            headers={"X-Customer-ID": str(owner)}, json={"content": "你好"},
        )
        assert unauthenticated.status_code == 401
        assert _login(client, "owner").status_code == 200
        created = client.post(
            f"/conversations/{conversation}/messages",
            headers={"X-Customer-ID": str(other)}, json={"content": "你好"},
        )
        assert created.status_code == 202
        assert client.get(
            f"/conversations/{conversation}", headers={"X-Customer-ID": str(other)}
        ).status_code == 200
        assert client.delete("/auth/session").status_code == 200
        assert client.get(f"/conversations/{conversation}").status_code == 401
        assert _login(client, "other").status_code == 200
        assert client.get(f"/conversations/{conversation}").status_code == 403
        assert client.get(f"/conversations/{conversation}/events").status_code == 403


def test_password_reset_invalidates_existing_session(tmp_path):
    customer = uuid.uuid4()
    with TestClient(_app(tmp_path)) as client:
        _account(client, "customer", customer)
        assert _login(client, "customer").status_code == 200
        assert client.get("/auth/session").status_code == 200

        _account(client, "customer", customer)
        assert client.get("/auth/session").status_code == 401
        assert _login(client, "customer").status_code == 200
        assert client.get("/auth/session").status_code == 200


def test_login_attempts_are_limited_per_username(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        responses = [
            client.post("/auth/session", json={"username": "unknown", "password": "wrong"})
            for _ in range(6)
        ]
        assert [response.status_code for response in responses] == [401] * 5 + [429]
        assert int(responses[-1].headers["Retry-After"]) >= 1


def test_message_rate_limit_checks_user_and_ip_before_creating_task(tmp_path):
    owner = uuid.uuid4()
    other = uuid.uuid4()
    with TestClient(_app(tmp_path, rate_limit_message_user=2, rate_limit_message_ip=3)) as client:
        _account(client, "owner", owner)
        _account(client, "other", other)
        assert _login(client, "owner").status_code == 200
        conversation = uuid.uuid4()
        statuses = [
            client.post(
                f"/api/v1/conversations/{conversation}/messages",
                json={"content": "你好"},
            ).status_code for _ in range(3)
        ]
        assert statuses == [202, 202, 429]
        assert _login(client, "other").status_code == 200
        allowed = client.post(
            f"/conversations/{uuid.uuid4()}/messages", json={"content": "你好"}
        )
        blocked = client.post(
            f"/conversations/{uuid.uuid4()}/messages", json={"content": "你好"}
        )
        assert allowed.status_code == 202
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1
        assert client.app.state.rate_limiter.values  # Both policies used the test store.


def test_evidence_upload_is_rate_limited_before_file_processing(tmp_path):
    customer = uuid.uuid4()
    with TestClient(_app(tmp_path, rate_limit_evidence_user=2)) as client:
        _account(client, "customer", customer)
        assert _login(client, "customer").status_code == 200
        url = "/after-sales/cases/UNKNOWN/evidence"
        statuses = [
            client.post(url, files={"file": ("proof.png", b"sample", "image/png")}).status_code
            for _ in range(3)
        ]
        assert statuses == [422, 422, 429]


@pytest.mark.asyncio
async def test_circuit_opens_and_allows_one_half_open_probe(monkeypatch):
    now = 100.0
    monkeypatch.setattr("backend.app.security.circuit_breaker.time.monotonic", lambda: now)
    registry = MemoryCircuitRegistry(threshold=5, window_seconds=30, open_seconds=30)
    breaker = registry.breaker("model")
    for _ in range(5):
        with pytest.raises(TimeoutError):
            async with breaker.guard():
                raise TimeoutError("provider timeout")
    with pytest.raises(CircuitOpenError) as blocked:
        async with breaker.guard():
            pass
    assert blocked.value.retry_after_seconds == 30
    now += 30
    async with breaker.guard():
        with pytest.raises(CircuitOpenError):
            async with breaker.guard():
                pass
    async with breaker.guard():
        pass


@pytest.mark.asyncio
async def test_http_oms_circuit_only_counts_server_failures(monkeypatch):
    registry = MemoryCircuitRegistry(threshold=2, window_seconds=30, open_seconds=30)
    settings = Settings(order_backend="http", order_oms_base_url="https://oms.example", _env_file=None)
    gateway = HttpOmsGateway(settings, registry.breaker("oms"))
    status = 404

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **_kwargs):
            return httpx.Response(status, request=httpx.Request("GET", url))

    monkeypatch.setattr("backend.app.orders.gateway.httpx.AsyncClient", FakeClient)
    assert await gateway.get_order("ORD-1", uuid.uuid4()) is None
    assert registry.failures.get("oms") is None
    status = 503
    for _ in range(2):
        with pytest.raises(OrderGatewayError):
            await gateway.get_order("ORD-1", uuid.uuid4())
    with pytest.raises(CircuitOpenError):
        await gateway.get_order("ORD-1", uuid.uuid4())


@pytest.mark.asyncio
async def test_open_model_circuit_retries_agent_command_without_spending_attempt(monkeypatch):
    run_id = uuid.uuid4()
    conversation_id = uuid.uuid4()

    class FakeMessage:
        body = json.dumps({
            "event_type": AGENT_REPLY_REQUESTED,
            "payload": {"run_id": str(run_id), "conversation_id": str(conversation_id)},
        }).encode()
        headers = {"x-agent-attempt": 4}
        message_id = "event-1"
        correlation_id = None
        acknowledged = False

        async def ack(self):
            self.acknowledged = True

        async def nack(self, **_kwargs):
            raise AssertionError("Message should be retried through a delayed queue")

    class FakeLeases:
        @asynccontextmanager
        async def acquire(self, _conversation_id):
            yield True

    class FakeBus:
        retry = None

        async def publish_retry(self, _payload, **kwargs):
            self.retry = kwargs

        async def publish_dead_letter(self, *_args, **_kwargs):
            raise AssertionError("An open circuit must not send the task to the DLQ")

    async def open_circuit(**_kwargs):
        raise CircuitOpenError("model", 30)

    monkeypatch.setattr("backend.app.workers.agent_reply.process_assistant_reply", open_circuit)
    bus = FakeBus()
    consumer = AgentReplyConsumer(
        settings=Settings(app_env="test", _env_file=None), database=None,
        broker=None, memory=None, responder=None, command_bus=bus, leases=FakeLeases(),
    )
    message = FakeMessage()
    await consumer.handle(message)
    assert message.acknowledged
    assert bus.retry["attempt"] == 4
    assert bus.retry["delay_tier"] == 2
