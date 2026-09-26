from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any


DEMO_CUSTOMER_ID = "00000000-0000-4000-8000-000000000001"
SECONDARY_DEMO_CUSTOMER_ID = "00000000-0000-4000-8000-000000000002"


DEMO_ORDERS: list[dict[str, Any]] = [
    {
        "order_id": "ORD-20260918-001",
        "customer_id": DEMO_CUSTOMER_ID,
        "status": "shipped",
        "payment_status": "paid",
        "amount": Decimal("399.00"),
        "currency": "CNY",
        "created_at": datetime.fromisoformat("2026-09-18T09:15:00+08:00"),
        "paid_at": datetime.fromisoformat("2026-09-18T09:16:00+08:00"),
        "items": [
            {
                "item_id": "ITEM-20260918-001-01",
                "sku_id": "SKU-DOWN-001",
                "product_name": "轻量羽绒服",
                "product_category": "服饰鞋包",
                "product_tags": [],
                "quantity": 1,
                "unit_price": Decimal("399.00"),
            },
        ],
        "shipments": [
            {
                "shipment_id": "SHP-20260918-001-01",
                "carrier": "顺丰速运",
                "tracking_number": "SF-DEMO-20260918001",
                "tracking_status": "运输中，预计明日送达",
                "shipped_at": datetime.fromisoformat("2026-09-18T18:00:00+08:00"),
                "signed_at": None,
                "updated_at": datetime.fromisoformat("2026-09-19T10:00:00+08:00"),
            }
        ],
    },
    {
        "order_id": "ORD-20260918-002",
        "customer_id": SECONDARY_DEMO_CUSTOMER_ID,
        "status": "paid",
        "payment_status": "paid",
        "amount": Decimal("269.00"),
        "currency": "CNY",
        "created_at": datetime.fromisoformat("2026-09-18T11:30:00+08:00"),
        "paid_at": datetime.fromisoformat("2026-09-18T11:31:00+08:00"),
        "items": [
            {
                "item_id": "ITEM-20260918-002-01",
                "sku_id": "SKU-SHOE-001",
                "product_name": "运动休闲鞋",
                "product_category": "服饰鞋包",
                "product_tags": [],
                "quantity": 1,
                "unit_price": Decimal("269.00"),
            }
        ],
        "shipments": [],
    },
    {
        "order_id": "ORD-20260920-003",
        "customer_id": DEMO_CUSTOMER_ID,
        "status": "completed",
        "payment_status": "paid",
        "amount": Decimal("188.00"),
        "currency": "CNY",
        "created_at": datetime.fromisoformat("2026-09-20T14:20:00+08:00"),
        "paid_at": datetime.fromisoformat("2026-09-20T14:21:00+08:00"),
        "items": [
            {
                "item_id": "ITEM-20260920-003-01",
                "sku_id": "SKU-CHERRY-001",
                "product_name": "智利车厘子礼盒",
                "product_category": "食品生鲜",
                "product_tags": ["生鲜食品"],
                "quantity": 1,
                "unit_price": Decimal("188.00"),
            }
        ],
        "shipments": [
            {
                "shipment_id": "SHP-20260920-003-01",
                "carrier": "顺丰速运",
                "tracking_number": "SF-DEMO-20260920003",
                "tracking_status": "已签收",
                "shipped_at": datetime.fromisoformat("2026-09-21T08:00:00+08:00"),
                "signed_at": datetime.fromisoformat("2026-09-24T10:30:00+08:00"),
                "updated_at": datetime.fromisoformat("2026-09-24T10:30:00+08:00"),
            }
        ],
    },
    {
        "order_id": "ORD-20260926-004",
        "customer_id": DEMO_CUSTOMER_ID,
        "status": "completed",
        "payment_status": "paid",
        "amount": Decimal("329.00"),
        "currency": "CNY",
        "created_at": datetime.fromisoformat("2026-09-25T09:10:00+08:00"),
        "paid_at": datetime.fromisoformat("2026-09-25T09:11:00+08:00"),
        "items": [
            {
                "item_id": "ITEM-20260926-004-01",
                "sku_id": "SKU-HEADPHONE-004",
                "product_name": "无线降噪耳机",
                "product_category": "数码电器",
                "product_tags": [],
                "quantity": 1,
                "unit_price": Decimal("329.00"),
            }
        ],
        "shipments": [
            {
                "shipment_id": "SHP-20260926-004-01",
                "carrier": "顺丰速运",
                "tracking_number": "SF-DEMO-20260926004",
                "tracking_status": "已签收",
                "shipped_at": datetime.fromisoformat("2026-09-25T14:00:00+08:00"),
                "signed_at": datetime.fromisoformat("2026-09-26T15:30:00+08:00"),
                "updated_at": datetime.fromisoformat("2026-09-26T15:30:00+08:00"),
            }
        ],
    },
    {
        "order_id": "ORD-20260926-005",
        "customer_id": DEMO_CUSTOMER_ID,
        "status": "paid",
        "payment_status": "paid",
        "amount": Decimal("299.00"),
        "currency": "CNY",
        "created_at": datetime.fromisoformat("2026-09-26T16:00:00+08:00"),
        "paid_at": datetime.fromisoformat("2026-09-26T16:01:00+08:00"),
        "items": [
            {
                "item_id": "ITEM-20260926-005-01",
                "sku_id": "SKU-SHOE-TEST-005",
                "product_name": "测试运动休闲鞋",
                "product_category": "服饰鞋包",
                "product_tags": [],
                "quantity": 1,
                "unit_price": Decimal("299.00"),
            }
        ],
        "shipments": [],
    },
    {
        "order_id": "ORD-20260926-006",
        "customer_id": DEMO_CUSTOMER_ID,
        "status": "completed",
        "payment_status": "paid",
        "amount": Decimal("199.00"),
        "currency": "CNY",
        "created_at": datetime.fromisoformat("2026-09-26T18:00:00+08:00"),
        "paid_at": datetime.fromisoformat("2026-09-26T18:01:00+08:00"),
        "items": [
            {
                "item_id": "ITEM-20260926-006-01",
                "sku_id": "SKU-SPEAKER-TEST-006",
                "product_name": "便携蓝牙音箱",
                "product_category": "数码电器",
                "product_tags": [],
                "quantity": 1,
                "unit_price": Decimal("199.00"),
            }
        ],
        "shipments": [
            {
                "shipment_id": "SHP-20260926-006-01",
                "carrier": "顺丰速运",
                "tracking_number": "SF-DEMO-20260926006",
                "tracking_status": "已签收",
                "shipped_at": datetime.fromisoformat("2026-09-26T19:00:00+08:00"),
                "signed_at": datetime.fromisoformat("2026-09-26T22:30:00+08:00"),
                "updated_at": datetime.fromisoformat("2026-09-26T22:30:00+08:00"),
            }
        ],
    },
]
