from __future__ import annotations

import json
import sqlite3
import uuid

from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.orders.demo_data import DEMO_CUSTOMER_ID


def make_app(database_path):
    return create_app(
        Settings(
            app_env="test",
            test_identity_header_enabled=True,
            database_url=f"sqlite+aiosqlite:///{database_path}",
            event_backend="memory",
            minio_enabled=False,
            agent_mode="mock",
            order_backend="mock",
            embedding_mode="hash",
            auto_create_schema=True,
        )
    )


def send(client: TestClient, conversation_id: uuid.UUID, customer_id: uuid.UUID, text: str):
    return client.post(
        f"/conversations/{conversation_id}/messages",
        headers={"X-Customer-ID": str(customer_id)},
        json={"content": text},
    )


def test_four_core_tools_and_redacted_trace_are_persisted(tmp_path):
    database_path = tmp_path / "tools.db"
    customer_id = uuid.UUID(DEMO_CUSTOMER_ID)
    prompts = [
        "查询订单 ORD-20260918-001",
        "物流 ORD-20260918-001 到哪了",
        "七天无理由退货政策是什么",
        "我要人工客服，请创建工单",
    ]
    with TestClient(make_app(database_path)) as client:
        for prompt in prompts:
            assert send(client, uuid.uuid4(), customer_id, prompt).status_code == 202

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT status, tool_call_count, trace FROM agent_runs ORDER BY created_at"
        ).fetchall()
        ticket_count = connection.execute("SELECT COUNT(*) FROM human_tickets").fetchone()[0]

    assert len(rows) == 4
    assert all(status == "completed" and count == 1 for status, count, _ in rows)
    tool_names = {
        event["tool_name"]
        for _, _, raw_trace in rows
        for event in json.loads(raw_trace)
        if event["event"] == "tool_started"
    }
    assert tool_names == {
        "get_order",
        "get_logistics",
        "search_policy",
        "create_human_ticket",
    }
    assert ticket_count == 1
    assert str(customer_id) not in "".join(raw_trace for _, _, raw_trace in rows)
