from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.database import Database
from backend.app.models import CustomerOrder


@dataclass(frozen=True)
class ExternalOperationResult:
    external_request_id: str
    status: str
    payload: dict


class OmsAdapter(Protocol):
    name: str

    async def cancel_order(
        self, *, order_no: str, idempotency_key: str
    ) -> ExternalOperationResult: ...


class PaymentAdapter(Protocol):
    name: str

    async def refund(
        self,
        *,
        order_no: str,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
    ) -> ExternalOperationResult: ...


def _external_id(prefix: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:20].upper()
    return f"{prefix}-{digest}"


class MockOmsAdapter:
    """Sandbox OMS adapter that persists the simulated order cancellation."""

    name = "mock_oms"

    def __init__(self, database: Database) -> None:
        self.database = database

    async def cancel_order(
        self, *, order_no: str, idempotency_key: str
    ) -> ExternalOperationResult:
        async with self.database.session_factory() as session:
            order = await session.scalar(
                select(CustomerOrder)
                .options(selectinload(CustomerOrder.shipments))
                .where(CustomerOrder.order_no == order_no)
                .with_for_update()
            )
            if order is None:
                raise RuntimeError("Mock OMS could not find the order")
            if order.status not in {"cancelled", "canceled", "closed"}:
                if order.shipments:
                    raise RuntimeError("Mock OMS refuses to cancel an order with shipment records")
                order.status = "cancelled"
            await session.commit()
        external_id = _external_id("MOCK-OMS-CANCEL", idempotency_key)
        return ExternalOperationResult(
            external_request_id=external_id,
            status="succeeded",
            payload={"order_id": order_no, "order_status": "cancelled"},
        )


class MockPaymentAdapter:
    """Sandbox payment adapter that persists a simulated full/partial refund."""

    name = "mock_payment"

    def __init__(self, database: Database) -> None:
        self.database = database

    async def refund(
        self,
        *,
        order_no: str,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
    ) -> ExternalOperationResult:
        async with self.database.session_factory() as session:
            order = await session.scalar(
                select(CustomerOrder)
                .where(CustomerOrder.order_no == order_no)
                .with_for_update()
            )
            if order is None:
                raise RuntimeError("Mock Payment could not find the order")
            if amount <= 0 or amount > order.amount:
                raise RuntimeError("Refund amount is outside the paid order amount")
            order.payment_status = "refunded" if amount == order.amount else "partially_refunded"
            await session.commit()
        external_id = _external_id("MOCK-REFUND", idempotency_key)
        return ExternalOperationResult(
            external_request_id=external_id,
            status="succeeded",
            payload={
                "order_id": order_no,
                "amount": str(amount),
                "currency": currency,
                "payment_status": (
                    "refunded" if amount == order.amount else "partially_refunded"
                ),
            },
        )
