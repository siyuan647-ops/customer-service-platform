from __future__ import annotations

from copy import deepcopy
from typing import Any


DEMO_CUSTOMER_ID = "00000000-0000-4000-8000-000000000001"
SECONDARY_DEMO_CUSTOMER_ID = "00000000-0000-4000-8000-000000000002"


_ORDERS: dict[str, dict[str, Any]] = {
    "ORD-20260918-001": {
        "customer_id": DEMO_CUSTOMER_ID,
        "order_id": "ORD-20260918-001",
        "status": "shipped",
        "payment_status": "paid",
        "product_name": "轻量羽绒服",
        "product_categories": ["服饰"],
        "quantity": 1,
        "amount": 399.0,
        "currency": "CNY",
        "carrier": "顺丰速运",
        "tracking_status": "运输中，预计明日送达",
        "signed_at": None,
    },
    "ORD-20260918-002": {
        "customer_id": SECONDARY_DEMO_CUSTOMER_ID,
        "order_id": "ORD-20260918-002",
        "status": "paid",
        "payment_status": "paid",
        "product_name": "运动休闲鞋",
        "product_categories": ["服饰"],
        "quantity": 1,
        "amount": 269.0,
        "currency": "CNY",
        "carrier": None,
        "tracking_status": "等待商家发货",
        "signed_at": None,
    },
    "ORD-20260920-003": {
        "customer_id": DEMO_CUSTOMER_ID,
        "order_id": "ORD-20260920-003",
        "status": "completed",
        "payment_status": "paid",
        "product_name": "智利车厘子礼盒",
        "product_categories": ["生鲜食品"],
        "quantity": 1,
        "amount": 188.0,
        "currency": "CNY",
        "carrier": "顺丰速运",
        "tracking_status": "已签收",
        "signed_at": "2026-09-24T10:30:00+08:00",
    },
}


async def lookup_order(order_id: str, customer_id: str = DEMO_CUSTOMER_ID) -> dict[str, Any]:
    """Return deterministic mock OMS data for the phase-0 spike."""
    normalized = order_id.strip().upper()
    order = _ORDERS.get(normalized)
    if order is None or order["customer_id"] != customer_id.strip().lower():
        return {
            "success": False,
            "error_code": "ORDER_NOT_FOUND",
            "message": "未找到该订单，请核对订单号。",
        }
    safe_order = deepcopy(order)
    safe_order.pop("customer_id")
    return {"success": True, "data": safe_order, "error_code": None}
