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

    async def update_shipping_address(
        self, *, order_no: str, address: dict, idempotency_key: str
    ) -> ExternalOperationResult: ...

    async def remind_shipment(
        self, *, order_no: str, idempotency_key: str
    ) -> ExternalOperationResult: ...

    async def create_after_sales_fulfillment(
        self, *, order_no: str, action: str, idempotency_key: str
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


class InvoiceAdapter(Protocol):
    name: str

    async def issue_invoice(
        self,
        *,
        order_no: str,
        invoice_data: dict,
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

    async def update_shipping_address(
        self, *, order_no: str, address: dict, idempotency_key: str
    ) -> ExternalOperationResult:
        return ExternalOperationResult(
            external_request_id=_external_id("MOCK-OMS-ADDRESS", idempotency_key),
            status="succeeded",
            payload={"order_id": order_no, "address": dict(address)},
        )

    async def remind_shipment(
        self, *, order_no: str, idempotency_key: str
    ) -> ExternalOperationResult:
        return ExternalOperationResult(
            external_request_id=_external_id("MOCK-OMS-REMIND", idempotency_key),
            status="accepted",
            payload={"order_id": order_no, "reminder_status": "accepted"},
        )

    async def create_after_sales_fulfillment(
        self, *, order_no: str, action: str, idempotency_key: str
    ) -> ExternalOperationResult:
        if action not in {"return_and_refund", "exchange", "reship", "repair"}:
            raise RuntimeError("Mock OMS does not support this after-sales action")
        return ExternalOperationResult(
            external_request_id=_external_id(
                f"MOCK-OMS-{action.upper()}", idempotency_key
            ),
            status="succeeded",
            payload={"order_id": order_no, "action": action, "status": "created"},
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


class MockInvoiceAdapter:
    name = "mock_invoice"

    async def issue_invoice(
        self,
        *,
        order_no: str,
        invoice_data: dict,
        idempotency_key: str,
    ) -> ExternalOperationResult:
        external_id = _external_id("MOCK-INVOICE", idempotency_key)
        return ExternalOperationResult(
            external_request_id=external_id,
            status="issued",
            payload={
                "order_id": order_no,
                "invoice_id": external_id,
                "invoice_type": invoice_data["invoice_type"],
                "download_url": f"https://example.invalid/invoices/{external_id}.pdf",
            },
        )
