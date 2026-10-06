from __future__ import annotations

import uuid
from typing import Protocol

import httpx
from backend.app.security.circuit_breaker import CircuitBreaker, optional_guard
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.agents.contracts import OrderFacts, OrderItemFacts, ShipmentFacts
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.models import CustomerOrder
from backend.app.orders.demo_data import DEMO_ORDERS


class OrderGatewayError(RuntimeError):
    pass


class OrderGateway(Protocol):
    async def get_order(self, order_id: str, customer_id: uuid.UUID) -> OrderFacts | None: ...

    async def get_logistics(
        self, order_id: str, customer_id: uuid.UUID
    ) -> list[ShipmentFacts] | None: ...


class MockOrderGateway:
    def __init__(self) -> None:
        self._orders = {item["order_id"]: item for item in DEMO_ORDERS}

    async def get_order(self, order_id: str, customer_id: uuid.UUID) -> OrderFacts | None:
        row = self._orders.get(order_id.strip().upper())
        if row is None or row["customer_id"] != str(customer_id):
            return None
        return OrderFacts.model_validate(
            {key: value for key, value in row.items() if key != "customer_id"}
        )

    async def get_logistics(
        self, order_id: str, customer_id: uuid.UUID
    ) -> list[ShipmentFacts] | None:
        order = await self.get_order(order_id, customer_id)
        return None if order is None else order.shipments


class PostgresOrderGateway:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def get_order(self, order_id: str, customer_id: uuid.UUID) -> OrderFacts | None:
        async with self.database.session_factory() as session:
            order = await session.scalar(
                select(CustomerOrder)
                .options(
                    selectinload(CustomerOrder.items),
                    selectinload(CustomerOrder.shipments),
                )
                .where(
                    CustomerOrder.order_no == order_id.strip().upper(),
                    CustomerOrder.customer_id == customer_id,
                )
            )
        if order is None:
            return None
        return OrderFacts(
            order_id=order.order_no,
            status=order.status,
            payment_status=order.payment_status,
            amount=order.amount,
            currency=order.currency,
            created_at=order.created_at,
            paid_at=order.paid_at,
            items=[
                OrderItemFacts(
                    item_id=item.item_no,
                    sku_id=item.sku_id,
                    product_name=item.product_name,
                    product_category=item.product_category,
                    product_tags=item.product_tags,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                )
                for item in order.items
            ],
            shipments=[
                ShipmentFacts(
                    shipment_id=shipment.shipment_no,
                    carrier=shipment.carrier,
                    tracking_number=shipment.tracking_number,
                    tracking_status=shipment.tracking_status,
                    shipped_at=shipment.shipped_at,
                    signed_at=shipment.signed_at,
                    updated_at=shipment.updated_at,
                )
                for shipment in order.shipments
            ],
        )

    async def get_logistics(
        self, order_id: str, customer_id: uuid.UUID
    ) -> list[ShipmentFacts] | None:
        order = await self.get_order(order_id, customer_id)
        return None if order is None else order.shipments


class HttpOmsGateway:
    def __init__(self, settings: Settings, breaker: CircuitBreaker | None = None) -> None:
        if not settings.order_oms_base_url:
            raise ValueError("ORDER_OMS_BASE_URL is required when ORDER_BACKEND=http")
        self.base_url = settings.order_oms_base_url.rstrip("/")
        self.api_key = settings.order_oms_api_key
        self.timeout = settings.order_oms_timeout_seconds
        self.breaker = breaker

    async def get_order(self, order_id: str, customer_id: uuid.UUID) -> OrderFacts | None:
        headers = {"X-Customer-ID": str(customer_id)}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with optional_guard(self.breaker):
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(
                        f"{self.base_url}/orders/{order_id.strip().upper()}",
                        headers=headers,
                    )
                if response.status_code in {403, 404}:
                    return None
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("OMS response must be a JSON object")
                return OrderFacts.model_validate(payload.get("data", payload))
        except (httpx.HTTPError, ValueError) as exc:
            raise OrderGatewayError("OMS returned an invalid response") from exc

    async def get_logistics(
        self, order_id: str, customer_id: uuid.UUID
    ) -> list[ShipmentFacts] | None:
        order = await self.get_order(order_id, customer_id)
        return None if order is None else order.shipments


def create_order_gateway(
    settings: Settings, database: Database, breaker: CircuitBreaker | None = None
) -> OrderGateway:
    if settings.order_backend == "mock":
        return MockOrderGateway()
    if settings.order_backend == "http":
        return HttpOmsGateway(settings, breaker)
    return PostgresOrderGateway(database)
