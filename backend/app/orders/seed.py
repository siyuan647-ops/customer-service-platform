from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from backend.app.config import get_settings
from backend.app.database import Database
from backend.app.models import CustomerOrder, OrderItem, Shipment
from backend.app.orders.demo_data import DEMO_ORDERS


@dataclass(frozen=True)
class SeedResult:
    created: int
    skipped: int


async def seed_demo_orders(database: Database) -> SeedResult:
    created = 0
    skipped = 0
    async with database.session_factory() as session:
        for data in DEMO_ORDERS:
            existing = await session.scalar(
                select(CustomerOrder).where(CustomerOrder.order_no == data["order_id"])
            )
            if existing is not None:
                if not existing.shipping_address:
                    existing.shipping_address = data.get("shipping_address", {})
                skipped += 1
                continue
            order = CustomerOrder(
                id=uuid.uuid4(),
                order_no=data["order_id"],
                customer_id=uuid.UUID(data["customer_id"]),
                status=data["status"],
                payment_status=data["payment_status"],
                amount=data["amount"],
                currency=data["currency"],
                shipping_address=data.get("shipping_address", {}),
                created_at=data["created_at"],
                paid_at=data["paid_at"],
            )
            order.items = [
                OrderItem(
                    item_no=item["item_id"],
                    sku_id=item["sku_id"],
                    product_name=item["product_name"],
                    product_category=item["product_category"],
                    product_tags=item["product_tags"],
                    quantity=item["quantity"],
                    unit_price=item["unit_price"],
                )
                for item in data["items"]
            ]
            order.shipments = [
                Shipment(
                    shipment_no=shipment["shipment_id"],
                    carrier=shipment["carrier"],
                    tracking_number=shipment["tracking_number"],
                    tracking_status=shipment["tracking_status"],
                    shipped_at=shipment["shipped_at"],
                    signed_at=shipment["signed_at"],
                    updated_at=shipment["updated_at"],
                )
                for shipment in data["shipments"]
            ]
            session.add(order)
            created += 1
        await session.commit()
    return SeedResult(created=created, skipped=skipped)


async def _run() -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    try:
        result = await seed_demo_orders(database)
        print(f"created={result.created} skipped={result.skipped}")
    finally:
        await database.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
