from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any


DEMO_CUSTOMER_ID = "00000000-0000-4000-8000-000000000001"
SECONDARY_DEMO_CUSTOMER_ID = "00000000-0000-4000-8000-000000000002"
_DEMO_NOW = datetime.now(UTC).replace(microsecond=0)


DEMO_ORDERS: list[dict[str, Any]] = [
    {
        "order_id": "ORD-20260918-001",
        "customer_id": DEMO_CUSTOMER_ID,
        "status": "shipped",
        "payment_status": "paid",
        "amount": Decimal("399.00"),
        "currency": "CNY",
        "shipping_address": {
            "recipient": "演示用户",
            "phone": "13800000001",
            "province": "上海市",
            "city": "上海市",
            "district": "浦东新区",
            "detail": "演示路1号",
        },
        "created_at": _DEMO_NOW - timedelta(hours=72),
        "paid_at": _DEMO_NOW - timedelta(hours=71, minutes=59),
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
                "shipped_at": _DEMO_NOW - timedelta(hours=48),
                "signed_at": None,
                "updated_at": _DEMO_NOW - timedelta(hours=1),
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
        "shipping_address": {
            "recipient": "演示用户二",
            "phone": "13800000002",
            "province": "上海市",
            "city": "上海市",
            "district": "徐汇区",
            "detail": "演示路2号",
        },
        "created_at": _DEMO_NOW - timedelta(hours=24),
        "paid_at": _DEMO_NOW - timedelta(hours=23, minutes=59),
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
        "shipping_address": {
            "recipient": "演示用户",
            "phone": "13800000001",
            "province": "上海市",
            "city": "上海市",
            "district": "浦东新区",
            "detail": "演示路1号",
        },
        "created_at": _DEMO_NOW - timedelta(hours=30),
        "paid_at": _DEMO_NOW - timedelta(hours=29, minutes=59),
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
                "shipped_at": _DEMO_NOW - timedelta(hours=6),
                "signed_at": _DEMO_NOW - timedelta(hours=3),
                "updated_at": _DEMO_NOW - timedelta(hours=3),
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
        "shipping_address": {
            "recipient": "演示用户",
            "phone": "13800000001",
            "province": "上海市",
            "city": "上海市",
            "district": "浦东新区",
            "detail": "演示路1号",
        },
        "created_at": _DEMO_NOW - timedelta(hours=30),
        "paid_at": _DEMO_NOW - timedelta(hours=29, minutes=59),
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
                "shipped_at": _DEMO_NOW - timedelta(hours=5),
                "signed_at": _DEMO_NOW - timedelta(hours=1),
                "updated_at": _DEMO_NOW - timedelta(hours=1),
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
        "shipping_address": {
            "recipient": "演示用户",
            "phone": "13800000001",
            "province": "上海市",
            "city": "上海市",
            "district": "浦东新区",
            "detail": "演示路1号",
        },
        "created_at": _DEMO_NOW - timedelta(hours=2),
        "paid_at": _DEMO_NOW - timedelta(hours=1, minutes=59),
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
        "shipping_address": {
            "recipient": "演示用户",
            "phone": "13800000001",
            "province": "上海市",
            "city": "上海市",
            "district": "浦东新区",
            "detail": "演示路1号",
        },
        "created_at": _DEMO_NOW - timedelta(hours=12),
        "paid_at": _DEMO_NOW - timedelta(hours=11, minutes=59),
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
                "shipped_at": _DEMO_NOW - timedelta(hours=6),
                "signed_at": _DEMO_NOW - timedelta(hours=2),
                "updated_at": _DEMO_NOW - timedelta(hours=2),
            }
        ],
    },
    {
        "order_id": "ORD-20260929-007",
        "customer_id": DEMO_CUSTOMER_ID,
        "status": "paid",
        "payment_status": "paid",
        "amount": Decimal("459.00"),
        "currency": "CNY",
        "shipping_address": {
            "recipient": "演示用户",
            "phone": "13800000001",
            "province": "上海市",
            "city": "上海市",
            "district": "浦东新区",
            "detail": "演示路1号",
        },
        "created_at": _DEMO_NOW - timedelta(minutes=40),
        "paid_at": _DEMO_NOW - timedelta(minutes=39),
        "items": [
            {
                "item_id": "ITEM-20260929-007-01",
                "sku_id": "SKU-CHAIR-TEST-007",
                "product_name": "人体工学办公椅",
                "product_category": "家居家电",
                "product_tags": [],
                "quantity": 1,
                "unit_price": Decimal("459.00"),
            }
        ],
        "shipments": [],
    },
    {
        "order_id": "ORD-20260929-008",
        "customer_id": DEMO_CUSTOMER_ID,
        "status": "completed",
        "payment_status": "paid",
        "amount": Decimal("699.00"),
        "currency": "CNY",
        "shipping_address": {
            "recipient": "演示用户",
            "phone": "13800000001",
            "province": "上海市",
            "city": "上海市",
            "district": "浦东新区",
            "detail": "演示路1号",
        },
        "created_at": _DEMO_NOW - timedelta(hours=3),
        "paid_at": _DEMO_NOW - timedelta(hours=2, minutes=59),
        "items": [
            {
                "item_id": "ITEM-20260929-008-01",
                "sku_id": "SKU-WATCH-TEST-008",
                "product_name": "智能运动手表",
                "product_category": "数码电器",
                "product_tags": [],
                "quantity": 1,
                "unit_price": Decimal("699.00"),
            }
        ],
        "shipments": [
            {
                "shipment_id": "SHP-20260929-008-01",
                "carrier": "顺丰速运",
                "tracking_number": "SF-DEMO-20260929008",
                "tracking_status": "已签收",
                "shipped_at": _DEMO_NOW - timedelta(hours=2),
                "signed_at": _DEMO_NOW - timedelta(minutes=30),
                "updated_at": _DEMO_NOW - timedelta(minutes=30),
            }
        ],
    },
]
