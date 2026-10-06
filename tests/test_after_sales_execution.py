from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.integrations import MockOmsAdapter, MockPaymentAdapter
from backend.app.main import create_app
from backend.app.models import (
    AfterSalesCase,
    AfterSalesOperation,
    CustomerOrder,
    OutboxEvent,
)
from backend.app.orders.demo_data import DEMO_CUSTOMER_ID, SECONDARY_DEMO_CUSTOMER_ID
from backend.app.orders.gateway import PostgresOrderGateway
from backend.app.orders.seed import seed_demo_orders
from backend.app.services.after_sales_execution import AfterSalesExecutionService
from backend.app.security.circuit_breaker import CircuitOpenError


ADMIN_HEADERS = {"X-Admin-Token": "test-admin", "X-Admin-ID": "agent-001"}


def _settings(path: Path) -> Settings:
    return Settings(
        app_env="test",
        test_identity_header_enabled=True,
        database_url=f"sqlite+aiosqlite:///{path}",
        event_backend="memory",
        minio_enabled=False,
        embedding_mode="hash",
        embedding_warmup_on_startup=False,
        order_backend="postgres",
        auto_create_schema=False,
        after_sales_admin_token="test-admin",
    )


async def _prepare(settings: Settings) -> None:
    database = Database(settings.database_url)
    try:
        await database.create_schema()
        await seed_demo_orders(database)
    finally:
        await database.dispose()


async def _process_all(settings: Settings) -> int:
    database = Database(settings.database_url)
    service = AfterSalesExecutionService(
        database,
        PostgresOrderGateway(database),
        MockOmsAdapter(database),
        MockPaymentAdapter(database),
        settings,
    )
    count = 0
    try:
        while await service.process_once():
            count += 1
    finally:
        await database.dispose()
    return count


def test_pre_shipment_cancel_and_refund_executes_once(tmp_path):
    settings = _settings(tmp_path / "cancel-refund.db")
    asyncio.run(_prepare(settings))
    customer_headers = {"X-Customer-ID": SECONDARY_DEMO_CUSTOMER_ID}

    with TestClient(create_app(settings)) as client:
        unauthorized = client.get("/admin/after-sales/cases")
        assert unauthorized.status_code == 403

        created = client.post(
            "/after-sales/cases",
            headers=customer_headers,
            json={
                "order_id": "ORD-20260918-002",
                "order_item_id": "ITEM-20260918-002-01",
                "case_type": "refund",
                "reason": "订单还没有发货，我不需要了",
                "idempotency_key": "real-business-flow-001",
            },
        )
        case_no = created.json()["case_no"]
        material = client.patch(
            f"/after-sales/cases/{case_no}/materials",
            headers=customer_headers,
            json={"problem_type": "未发货取消订单"},
        )
        submitted = client.post(
            f"/after-sales/cases/{case_no}/submit", headers=customer_headers
        )
        review = client.post(
            f"/admin/after-sales/cases/{case_no}/start-review",
            headers=ADMIN_HEADERS,
        )
        duplicate = client.post(
            "/after-sales/cases",
            headers=customer_headers,
            json={
                "order_id": "ORD-20260918-002",
                "order_item_id": "ITEM-20260918-002-01",
                "case_type": "refund",
                "reason": "再次申请同一件商品退款",
                "idempotency_key": "real-business-flow-duplicate",
            },
        )
        approved = client.post(
            f"/admin/after-sales/cases/{case_no}/approve",
            headers=ADMIN_HEADERS,
            json={
                "action": "cancel_and_refund",
                "refund_amount": "269.00",
                "reason": "未发货订单，批准取消并原路退款",
            },
        )

    assert created.status_code == 201
    assert material.status_code == 200
    assert submitted.json()["status"] == "SUBMITTED"
    assert review.json()["status"] == "UNDER_REVIEW"
    assert duplicate.status_code == 201
    assert duplicate.json()["case_no"] == case_no
    assert duplicate.json()["status"] == "UNDER_REVIEW"
    assert approved.json()["status"] == "APPROVED"

    assert asyncio.run(_process_all(settings)) == 1
    assert asyncio.run(_process_all(settings)) == 0

    with TestClient(create_app(settings)) as client:
        completed = client.get(
            f"/admin/after-sales/cases/{case_no}", headers=ADMIN_HEADERS
        )

    payload = completed.json()
    assert completed.status_code == 200
    assert payload["status"] == "COMPLETED"
    assert [item["operation_type"] for item in payload["operations"]] == [
        "cancel_order",
        "refund_payment",
    ]
    assert all(item["status"] == "SUCCEEDED" for item in payload["operations"])

    async def verify_database() -> None:
        database = Database(settings.database_url)
        try:
            async with database.session_factory() as session:
                order = await session.scalar(
                    select(CustomerOrder).where(
                        CustomerOrder.order_no == "ORD-20260918-002"
                    )
                )
                assert order is not None
                assert order.status == "cancelled"
                assert order.payment_status == "refunded"
                assert await session.scalar(
                        select(OutboxEvent.status).where(
                            OutboxEvent.aggregate_id
                            == uuid.UUID(completed.json()["id"])
                        )
                ) == "PROCESSED"
                operations = list(
                    await session.scalars(select(AfterSalesOperation))
                )
                assert len(operations) == 2
        finally:
            await database.dispose()

    asyncio.run(verify_database())


def test_signed_order_refund_only_executes_payment_without_oms_cancel(tmp_path):
    settings = _settings(tmp_path / "refund-only.db")
    asyncio.run(_prepare(settings))
    customer_headers = {"X-Customer-ID": DEMO_CUSTOMER_ID}

    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/after-sales/cases",
            headers=customer_headers,
            json={
                "order_id": "ORD-20260926-004",
                "order_item_id": "ITEM-20260926-004-01",
                "case_type": "refund",
                "reason": "无线降噪耳机外壳破损",
                "idempotency_key": "real-business-flow-002",
            },
        )
        case_no = created.json()["case_no"]
        client.patch(
            f"/after-sales/cases/{case_no}/materials",
            headers=customer_headers,
            json={"problem_type": "运输破损"},
        )
        client.post(
            f"/after-sales/cases/{case_no}/evidence",
            headers=customer_headers,
            files={"file": ("damage.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
        submitted = client.post(
            f"/after-sales/cases/{case_no}/submit", headers=customer_headers
        )
        client.post(
            f"/admin/after-sales/cases/{case_no}/start-review",
            headers=ADMIN_HEADERS,
        )
        approved = client.post(
            f"/admin/after-sales/cases/{case_no}/approve",
            headers=ADMIN_HEADERS,
            json={
                "action": "refund_only",
                "refund_amount": "100.00",
                "reason": "破损凭证审核通过，批准部分退款",
            },
        )

    assert submitted.json()["status"] == "SUBMITTED"
    assert approved.json()["status"] == "APPROVED"
    assert asyncio.run(_process_all(settings)) == 1

    with TestClient(create_app(settings)) as client:
        completed = client.get(
            f"/admin/after-sales/cases/{case_no}", headers=ADMIN_HEADERS
        ).json()

    assert completed["status"] == "COMPLETED"
    assert completed["evidence"][0]["analysis_status"] == "PENDING"
    assert completed["evidence"][0]["analysis_result"] == {}
    assert [item["operation_type"] for item in completed["operations"]] == [
        "refund_payment"
    ]
    assert completed["operations"][0]["status"] == "SUCCEEDED"


@pytest.mark.parametrize("action", ["exchange", "reship", "repair"])
def test_non_refund_after_sales_actions_execute_through_mock_oms(tmp_path, action):
    settings = _settings(tmp_path / f"{action}.db")
    asyncio.run(_prepare(settings))
    customer_headers = {"X-Customer-ID": DEMO_CUSTOMER_ID}

    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/after-sales/cases",
            headers=customer_headers,
            json={
                "order_id": "ORD-20260926-004",
                "order_item_id": "ITEM-20260926-004-01",
                "case_type": action,
                "reason": f"申请{action}",
                "idempotency_key": f"execute-{action}-001",
            },
        )
        case_no = created.json()["case_no"]
        client.patch(
            f"/after-sales/cases/{case_no}/materials",
            headers=customer_headers,
            json={"case_type": action, "problem_type": "功能故障"},
        )
        client.post(
            f"/after-sales/cases/{case_no}/evidence",
            headers=customer_headers,
            files={"file": ("fault.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
        client.post(f"/after-sales/cases/{case_no}/submit", headers=customer_headers)
        client.post(
            f"/admin/after-sales/cases/{case_no}/start-review",
            headers=ADMIN_HEADERS,
        )
        approved = client.post(
            f"/admin/after-sales/cases/{case_no}/approve",
            headers=ADMIN_HEADERS,
            json={
                "action": action,
                "refund_amount": None,
                "reason": "材料审核通过",
            },
        )

    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"
    assert asyncio.run(_process_all(settings)) == 1

    with TestClient(create_app(settings)) as client:
        completed = client.get(
            f"/admin/after-sales/cases/{case_no}", headers=ADMIN_HEADERS
        ).json()
    assert completed["status"] == "COMPLETED"
    assert [item["operation_type"] for item in completed["operations"]] == [
        f"create_{action}"
    ]


def test_database_rejects_two_active_cases_for_same_order_item(tmp_path):
    settings = _settings(tmp_path / "active-case-constraint.db")
    asyncio.run(_prepare(settings))

    async def verify_constraint() -> None:
        database = Database(settings.database_url)
        common = {
            "customer_id": uuid.UUID(SECONDARY_DEMO_CUSTOMER_ID),
            "order_no": "ORD-20260918-002",
            "order_item_no": "ITEM-20260918-002-01",
            "case_type": "refund",
            "reason": "验证活动案件唯一约束",
        }
        try:
            async with database.session_factory() as session:
                session.add(
                    AfterSalesCase(
                        **common,
                        case_no="AS-20260926-CONSTRAINT01",
                        status="UNDER_REVIEW",
                        idempotency_key="constraint-case-001",
                    )
                )
                await session.commit()

            async with database.session_factory() as session:
                session.add(
                    AfterSalesCase(
                        **common,
                        case_no="AS-20260926-CONSTRAINT02",
                        status="APPROVED",
                        idempotency_key="constraint-case-002",
                    )
                )
                with pytest.raises(IntegrityError):
                    await session.commit()
                await session.rollback()
        finally:
            await database.dispose()

    asyncio.run(verify_constraint())


def test_open_oms_circuit_defers_after_sales_without_spending_attempt(tmp_path):
    settings = _settings(tmp_path / "deferred.db")
    asyncio.run(_prepare(settings))
    customer_headers = {"X-Customer-ID": SECONDARY_DEMO_CUSTOMER_ID}
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/after-sales/cases", headers=customer_headers,
            json={
                "order_id": "ORD-20260918-002",
                "order_item_id": "ITEM-20260918-002-01",
                "case_type": "refund",
                "reason": "订单未发货，需要取消",
                "idempotency_key": "deferred-oms-001",
            },
        )
        assert created.status_code == 201
        case_no = created.json()["case_no"]
        assert client.patch(
            f"/after-sales/cases/{case_no}/materials", headers=customer_headers,
            json={"problem_type": "未发货取消订单"},
        ).status_code == 200
        assert client.post(
            f"/after-sales/cases/{case_no}/submit", headers=customer_headers
        ).status_code == 200
        assert client.post(
            f"/admin/after-sales/cases/{case_no}/start-review", headers=ADMIN_HEADERS
        ).status_code == 200
        assert client.post(
            f"/admin/after-sales/cases/{case_no}/approve", headers=ADMIN_HEADERS,
            json={
                "action": "cancel_and_refund", "refund_amount": "269.00",
                "reason": "测试熔断延期",
            },
        ).status_code == 200

    class OpenOms:
        async def get_order(self, *_args):
            raise CircuitOpenError("oms", 30)

    async def verify():
        database = Database(settings.database_url)
        service = AfterSalesExecutionService(
            database, OpenOms(), MockOmsAdapter(database), MockPaymentAdapter(database),
            settings,
        )
        try:
            assert await service.process_once() is True
            async with database.session_factory() as session:
                event = await session.scalar(select(OutboxEvent).where(
                    OutboxEvent.event_type == "after_sales.execute"
                ))
                case = await session.scalar(select(AfterSalesCase).where(
                    AfterSalesCase.case_no == case_no
                ))
            assert event is not None and event.status == "PENDING"
            assert event.attempt_count == 0
            assert event.available_at is not None
            assert case is not None and case.status == "APPROVED"
        finally:
            await database.dispose()

    asyncio.run(verify())
