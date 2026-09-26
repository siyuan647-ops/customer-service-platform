from __future__ import annotations

import json
import sqlite3
import uuid

from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.orders.demo_data import DEMO_CUSTOMER_ID


FRESH_POLICY = """【基础信息】
- 政策标题：特殊商品退款例外规则
- 生效日期：2026-02-01
- 失效日期：-
- 适用商品分类：生鲜食品、定制商品、虚拟商品、贴身用品

1. 生鲜食品类
1.1 生鲜商品不支持七天无理由退货
1.2 签收时发现变质、破损、缺斤少两，当场拒收或2小时内举证，可申请退款或补发
"""


def make_app(database_path):
    return create_app(
        Settings(
            app_env="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            event_backend="memory",
            minio_enabled=False,
            agent_mode="mock",
            order_backend="mock",
            embedding_mode="hash",
            auto_create_schema=True,
        )
    )


def test_after_sales_workflow_uses_order_policy_and_expert_agent(tmp_path):
    database_path = tmp_path / "after-sales.db"
    conversation_id = uuid.uuid4()
    customer_id = uuid.UUID(DEMO_CUSTOMER_ID)
    with TestClient(make_app(database_path)) as client:
        uploaded = client.post(
            "/knowledge/documents",
            files={"file": ("特殊商品退款例外规则.md", FRESH_POLICY.encode(), "text/markdown")},
        )
        accepted = client.post(
            f"/conversations/{conversation_id}/messages",
            headers={"X-Customer-ID": str(customer_id)},
            json={"content": "订单 ORD-20260920-003 的生鲜商品破损了，可以退款吗？"},
        )
        conversation = client.get(
            f"/conversations/{conversation_id}",
            headers={"X-Customer-ID": str(customer_id)},
        )

    assert uploaded.status_code == 201
    assert accepted.status_code == 202
    answer = conversation.json()["messages"][-1]["content"]
    assert "暂不符合" in answer
    assert "超过签收后2小时" in answer
    assert "【来源：特殊商品退款例外规则.md，1. 生鲜食品类】" in answer

    with sqlite3.connect(database_path) as connection:
        tool_call_count, raw_trace = connection.execute(
            "SELECT tool_call_count, trace FROM agent_runs"
        ).fetchone()
    trace = json.loads(raw_trace)
    assert tool_call_count == 2
    assert [
        event["tool_name"] for event in trace if event["event"] == "tool_started"
    ] == ["get_order", "search_policy"]
    assert any(event["event"] == "deterministic_rule_applied" for event in trace)
    assert any(
        event.get("state") == "RESULT_VALIDATION"
        for event in trace
        if event["event"] == "workflow_state_changed"
    )


def test_concrete_after_sales_request_without_order_waits_for_user(tmp_path):
    database_path = tmp_path / "missing-order.db"
    conversation_id = uuid.uuid4()
    customer_id = uuid.UUID(DEMO_CUSTOMER_ID)
    with TestClient(make_app(database_path)) as client:
        accepted = client.post(
            f"/conversations/{conversation_id}/messages",
            headers={"X-Customer-ID": str(customer_id)},
            json={"content": "我买的商品破损了，想退款"},
        )
        conversation = client.get(
            f"/conversations/{conversation_id}",
            headers={"X-Customer-ID": str(customer_id)},
        )

    assert accepted.status_code == 202
    assert "请提供订单号" in conversation.json()["messages"][-1]["content"]
    with sqlite3.connect(database_path) as connection:
        tool_call_count = connection.execute(
            "SELECT tool_call_count FROM agent_runs"
        ).fetchone()[0]
    assert tool_call_count == 0


def test_follow_up_reloads_order_id_from_postgres_when_memory_is_empty(tmp_path):
    database_path = tmp_path / "history.db"
    conversation_id = uuid.uuid4()
    customer_id = uuid.UUID(DEMO_CUSTOMER_ID)
    headers = {"X-Customer-ID": str(customer_id)}
    with TestClient(make_app(database_path)) as client:
        first = client.post(
            f"/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": "查询订单 ORD-20260918-001"},
        )
        # Simulate an expired or lost Redis short-term context. PostgreSQL is
        # still the durable source and must repopulate the cache.
        client.app.state.conversation_memory._turns.clear()
        client.app.state.conversation_memory._expires_at.clear()
        second = client.post(
            f"/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": "物流到哪了？"},
        )
        conversation = client.get(
            f"/conversations/{conversation_id}", headers=headers
        )

    assert first.status_code == 202
    assert second.status_code == 202
    answer = conversation.json()["messages"][-1]["content"]
    assert "ORD-20260918-001" in answer
    assert "运输中" in answer
