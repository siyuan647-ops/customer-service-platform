from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.orders.demo_data import DEMO_CUSTOMER_ID


def make_app(tmp_path):
    return create_app(
        Settings(
            app_env="test",
            test_identity_header_enabled=True,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}",
            event_backend="memory",
            minio_enabled=False,
            agent_mode="mock",
            order_backend="mock",
            embedding_mode="hash",
            auto_create_schema=True,
        )
    )


def _message(client: TestClient, conversation_id: uuid.UUID, content: str):
    return client.post(
        f"/conversations/{conversation_id}/messages",
        headers={"X-Customer-ID": DEMO_CUSTOMER_ID},
        json={"content": content},
    )


def test_address_change_uses_structured_form_and_customer_scope(tmp_path):
    conversation_id = uuid.uuid4()
    headers = {"X-Customer-ID": DEMO_CUSTOMER_ID}
    with TestClient(make_app(tmp_path)) as client:
        accepted = _message(
            client,
            conversation_id,
            "修改订单 ORD-20260926-005 的收货地址",
        )
        requests = client.get(
            "/service-requests",
            headers=headers,
            params={"conversation_id": str(conversation_id)},
        ).json()
        assert accepted.status_code == 202
        assert len(requests) == 1
        assert requests[0]["request_type"] == "address_change"
        assert requests[0]["status"] == "DRAFT"

        updated = client.post(
            f"/service-requests/{requests[0]['request_no']}/address",
            headers=headers,
            json={
                "recipient": "张三",
                "phone": "13800000001",
                "province": "上海市",
                "city": "上海市",
                "district": "浦东新区",
                "detail": "世纪大道100号",
            },
        )
        denied = client.get(
            f"/service-requests/{requests[0]['request_no']}",
            headers={"X-Customer-ID": str(uuid.uuid4())},
        )

    assert updated.status_code == 200
    assert updated.json()["status"] == "COMPLETED"
    assert denied.status_code == 404


def test_shipment_reminder_is_deduplicated_for_24_hours(tmp_path):
    conversation_id = uuid.uuid4()
    headers = {"X-Customer-ID": DEMO_CUSTOMER_ID}
    with TestClient(make_app(tmp_path)) as client:
        first = _message(
            client,
            conversation_id,
            "帮我催发货，订单 ORD-20260926-005",
        )
        second = _message(
            client,
            conversation_id,
            "订单 ORD-20260926-005 再催一次发货",
        )
        requests = client.get(
            "/service-requests",
            headers=headers,
            params={"order_id": "ORD-20260926-005", "request_type": "shipment_reminder"},
        ).json()

    assert first.status_code == 202
    assert second.status_code == 202
    assert len(requests) == 1
    assert requests[0]["status"] == "COMPLETED"


def test_invoice_application_and_query(tmp_path):
    conversation_id = uuid.uuid4()
    headers = {"X-Customer-ID": DEMO_CUSTOMER_ID}
    with TestClient(make_app(tmp_path)) as client:
        accepted = _message(
            client,
            conversation_id,
            "给订单 ORD-20260926-004 申请发票",
        )
        requests = client.get(
            "/service-requests",
            headers=headers,
            params={"conversation_id": str(conversation_id)},
        ).json()
        issued = client.post(
            f"/service-requests/{requests[0]['request_no']}/invoice",
            headers=headers,
            json={
                "invoice_type": "electronic_general",
                "title_type": "personal",
                "title": "张三",
                "email": "demo@example.com",
            },
        )
        queried = _message(
            client,
            conversation_id,
            "查询订单 ORD-20260926-004 的发票状态",
        )
        conversation = client.get(
            f"/conversations/{conversation_id}", headers=headers
        ).json()

    assert accepted.status_code == 202
    assert issued.status_code == 200
    assert issued.json()["status"] == "COMPLETED"
    assert issued.json()["result_payload"]["download_url"]
    assert queried.status_code == 202
    assert "COMPLETED" in conversation["messages"][-1]["content"]


def test_after_sales_status_is_read_from_persisted_case(tmp_path):
    conversation_id = uuid.uuid4()
    headers = {"X-Customer-ID": DEMO_CUSTOMER_ID}
    with TestClient(make_app(tmp_path)) as client:
        created = client.post(
            "/after-sales/cases",
            headers=headers,
            json={
                "order_id": "ORD-20260926-004",
                "order_item_id": "ITEM-20260926-004-01",
                "case_type": "repair",
                "reason": "耳机无法正常使用，需要维修",
                "idempotency_key": "repair-status-test-001",
            },
        )
        queried = _message(
            client,
            conversation_id,
            "查询订单 ORD-20260926-004 的售后进度",
        )
        conversation = client.get(
            f"/conversations/{conversation_id}", headers=headers
        ).json()

    assert created.status_code == 201
    assert queried.status_code == 202
    assert created.json()["case_no"] in conversation["messages"][-1]["content"]
