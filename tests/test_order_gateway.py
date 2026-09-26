from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from backend.app.agents.contracts import OrderFacts, OrderItemFacts, ProductCategory, ProductTag
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.models import CustomerOrder, OrderItem, Shipment
from backend.app.orchestration.workflow import CustomerServiceWorkflow
from backend.app.orders.demo_data import DEMO_CUSTOMER_ID
from backend.app.orders.gateway import (
    MockOrderGateway,
    PostgresOrderGateway,
    create_order_gateway,
)
from backend.app.orders.seed import seed_demo_orders


@pytest.mark.asyncio
async def test_postgres_seed_is_idempotent_and_gateway_enforces_owner(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'orders.db'}")
    try:
        await database.create_schema()
        first = await seed_demo_orders(database)
        second = await seed_demo_orders(database)

        assert first.created == 6
        assert first.skipped == 0
        assert second.created == 0
        assert second.skipped == 6

        async with database.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(CustomerOrder)) == 6
            assert await session.scalar(select(func.count()).select_from(OrderItem)) == 6
            assert await session.scalar(select(func.count()).select_from(Shipment)) == 4

        gateway = PostgresOrderGateway(database)
        order = await gateway.get_order(
            "ord-20260920-003", uuid.UUID(DEMO_CUSTOMER_ID)
        )
        hidden = await gateway.get_order(
            "ORD-20260920-003", uuid.UUID("00000000-0000-4000-8000-000000000099")
        )

        assert order is not None
        assert order.items[0].product_category == ProductCategory.FOOD_FRESH
        assert order.items[0].product_tags == [ProductTag.FRESH]
        assert order.shipments[0].tracking_status == "已签收"
        assert hidden is None

        recent = await gateway.get_order(
            "ORD-20260926-004", uuid.UUID(DEMO_CUSTOMER_ID)
        )
        assert recent is not None
        assert recent.items[0].product_category == ProductCategory.DIGITAL
        assert recent.shipments[0].signed_at.replace(tzinfo=None) == datetime.fromisoformat(
            "2026-09-26T15:30:00"
        )

        refund_test_order = await gateway.get_order(
            "ORD-20260926-005", uuid.UUID(DEMO_CUSTOMER_ID)
        )
        assert refund_test_order is not None
        assert refund_test_order.status == "paid"
        assert refund_test_order.payment_status == "paid"
        assert refund_test_order.shipments == []

        signed_refund_order = await gateway.get_order(
            "ORD-20260926-006", uuid.UUID(DEMO_CUSTOMER_ID)
        )
        assert signed_refund_order is not None
        assert signed_refund_order.status == "completed"
        assert signed_refund_order.payment_status == "paid"
        assert signed_refund_order.items[0].product_category == ProductCategory.DIGITAL
        assert signed_refund_order.shipments[0].tracking_status == "已签收"
    finally:
        await database.dispose()


def test_order_gateway_factory_selects_mock_backend(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'factory.db'}")
    gateway = create_order_gateway(Settings(order_backend="mock"), database)

    assert isinstance(gateway, MockOrderGateway)


def test_multi_item_order_requires_an_explicit_item_when_name_is_not_mentioned():
    order = OrderFacts(
        order_id="ORD-20260922-004",
        status="completed",
        payment_status="paid",
        amount=Decimal("498.00"),
        currency="CNY",
        created_at=datetime.now(UTC),
        items=[
            OrderItemFacts(
                item_id="ITEM-20260922-004-01",
                sku_id="SKU-1",
                product_name="羽绒服",
                product_category=ProductCategory.CLOTHING,
                quantity=1,
                unit_price=Decimal("399.00"),
            ),
            OrderItemFacts(
                item_id="ITEM-20260922-004-02",
                sku_id="SKU-2",
                product_name="电热水壶",
                product_category=ProductCategory.HOME_APPLIANCE,
                quantity=1,
                unit_price=Decimal("99.00"),
            ),
        ],
    )
    from backend.app.agents.contracts import SupervisorPlan

    plan = SupervisorPlan(intent="after_sales", order_id=order.order_id)

    assert CustomerServiceWorkflow._select_order_item(order, plan, "这个坏了") is None
    selected = CustomerServiceWorkflow._select_order_item(
        order, plan, "羽绒服坏了，想申请退款"
    )
    assert selected is not None
    assert selected.item_id == "ITEM-20260922-004-01"


def test_policy_scope_prefers_specific_product_tag():
    assert (
        CustomerServiceWorkflow._policy_scope(
            ProductCategory.CLOTHING, [ProductTag.PERSONAL]
        )
        == "贴身用品"
    )
    assert (
        CustomerServiceWorkflow._policy_scope(ProductCategory.FOOD_FRESH, [])
        == "生鲜食品"
    )
