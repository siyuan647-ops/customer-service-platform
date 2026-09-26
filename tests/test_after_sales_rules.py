from datetime import UTC, datetime, timedelta
from decimal import Decimal

from backend.app.agents.contracts import (
    OrderFacts,
    OrderItemFacts,
    PolicyEvidence,
    ProductCategory,
    ProductTag,
    ShipmentFacts,
)
from backend.app.services.after_sales_rules import AfterSalesRuleEngine


def _evidence(
    *,
    chunk_id: str,
    content: str,
    product_categories: list[str],
    source_filename: str,
) -> PolicyEvidence:
    return PolicyEvidence(
        document_id=f"doc-{chunk_id}",
        chunk_id=chunk_id,
        title=source_filename.removesuffix(".md"),
        section="规则",
        content=content,
        product_categories=product_categories,
        source_filename=source_filename,
        score=0.5,
    )


def _order(
    signed_at: datetime | None,
    *,
    status: str = "completed",
    payment_status: str = "paid",
    shipped_at: datetime | None = None,
) -> OrderFacts:
    return OrderFacts(
        order_id="ORD-20260920-003",
        status=status,
        payment_status=payment_status,
        amount=Decimal("188.00"),
        currency="CNY",
        created_at=datetime(2026, 9, 20, tzinfo=UTC),
        items=[
            OrderItemFacts(
                item_id="ITEM-1",
                sku_id="SKU-1",
                product_name="车厘子",
                product_category=ProductCategory.FOOD_FRESH,
                product_tags=[ProductTag.FRESH],
                quantity=1,
                unit_price=Decimal("188.00"),
            )
        ],
        shipments=[
            ShipmentFacts(
                shipment_id="SHIP-1",
                tracking_status="已签收",
                shipped_at=shipped_at,
                signed_at=signed_at,
                updated_at=signed_at or datetime(2026, 9, 24, tzinfo=UTC),
            )
        ],
    )


def test_product_specific_policy_overrides_generic_policy_for_same_claim():
    specific = _evidence(
        chunk_id="specific",
        content="生鲜商品破损，应在签收后2小时内举证，可申请退款。",
        product_categories=["生鲜食品"],
        source_filename="特殊商品退款例外规则.md",
    )
    generic = _evidence(
        chunk_id="generic",
        content="全品类运输破损，应在签收后48小时内举证，可申请退款。",
        product_categories=["全品类"],
        source_filename="物流异常处理办法.md",
    )

    selected = AfterSalesRuleEngine.select_applicable_evidence(
        [generic, specific],
        product_scope="生鲜食品",
    )

    assert [item.chunk_id for item in selected] == ["specific"]


def test_deadline_is_computed_from_trusted_signed_and_request_times():
    signed_at = datetime(2026, 9, 24, 2, 30, tzinfo=UTC)
    evidence = [
        _evidence(
            chunk_id="fresh",
            content="这是用于向用户解释规则的政策正文，不包含机器可解析的小时数。",
            product_categories=["生鲜食品"],
            source_filename="特殊商品退款例外规则.md",
        )
    ]

    within = AfterSalesRuleEngine.evaluate_deadline(
        _order(signed_at),
        product_category=ProductCategory.FOOD_FRESH,
        requested_at=signed_at + timedelta(minutes=30),
        evidence=evidence,
    )
    expired = AfterSalesRuleEngine.evaluate_deadline(
        _order(signed_at),
        product_category=ProductCategory.FOOD_FRESH,
        requested_at=signed_at + timedelta(hours=3),
        evidence=evidence,
    )

    assert within.status == "within_deadline"
    assert within.deadline_at == signed_at + timedelta(hours=2)
    assert within.remaining_seconds == 90 * 60
    assert expired.status == "expired"
    assert expired.remaining_seconds == 0


def test_expired_deadline_produces_deterministic_ineligible_decision():
    signed_at = datetime(2026, 9, 24, 2, 30, tzinfo=UTC)
    evidence = [
        _evidence(
            chunk_id="fresh",
            content="生鲜商品破损，应在签收后2小时内举证。",
            product_categories=["生鲜食品"],
            source_filename="特殊商品退款例外规则.md",
        )
    ]
    deadline = AfterSalesRuleEngine.evaluate_deadline(
        _order(signed_at),
        product_category=ProductCategory.FOOD_FRESH,
        requested_at=signed_at + timedelta(hours=3),
        evidence=evidence,
    )

    decision = AfterSalesRuleEngine.expired_decision(deadline)

    assert decision.decision == "ineligible"
    assert decision.reason_code == "EVIDENCE_DEADLINE_EXPIRED"
    assert decision.should_handoff is False


def test_non_fresh_category_uses_fixed_48_hour_window_without_policy_parsing():
    signed_at = datetime(2026, 9, 24, 2, 30, tzinfo=UTC)

    deadline = AfterSalesRuleEngine.evaluate_deadline(
        _order(signed_at),
        product_category=ProductCategory.DIGITAL,
        requested_at=signed_at + timedelta(hours=47),
        evidence=[],
    )

    assert deadline.status == "within_deadline"
    assert deadline.window_hours == 48
    assert deadline.deadline_at == signed_at + timedelta(hours=48)


def test_order_state_rejects_unpaid_cancelled_and_refunded_orders():
    unpaid = AfterSalesRuleEngine.evaluate_order_state(
        _order(None, status="pending_payment", payment_status="unpaid")
    )
    cancelled = AfterSalesRuleEngine.evaluate_order_state(
        _order(None, status="cancelled", payment_status="paid")
    )
    refunded = AfterSalesRuleEngine.evaluate_order_state(
        _order(None, status="completed", payment_status="refunded")
    )

    assert unpaid is not None and unpaid.reason_code == "ORDER_NOT_PAID"
    assert cancelled is not None and cancelled.reason_code == "ORDER_CANCELLED_OR_CLOSED"
    assert refunded is not None and refunded.reason_code == "ORDER_ALREADY_REFUNDED"
    assert {unpaid.decision, cancelled.decision, refunded.decision} == {"ineligible"}


def test_order_state_routes_paid_unshipped_and_shipped_unsigned_orders():
    paid_unshipped = AfterSalesRuleEngine.evaluate_order_state(
        _order(None, status="paid", payment_status="paid")
    )
    shipped_unsigned = AfterSalesRuleEngine.evaluate_order_state(
        _order(
            None,
            status="shipped",
            payment_status="paid",
            shipped_at=datetime(2026, 9, 25, tzinfo=UTC),
        )
    )

    assert paid_unshipped is not None
    assert paid_unshipped.reason_code == "PRE_SHIPMENT_REFUND_ALLOWED"
    assert paid_unshipped.decision == "eligible"
    assert shipped_unsigned is not None
    assert shipped_unsigned.reason_code == "ORDER_NOT_SIGNED"
    assert shipped_unsigned.decision == "ineligible"


def test_signed_paid_order_enters_normal_after_sales_flow():
    assert (
        AfterSalesRuleEngine.evaluate_order_state(
            _order(datetime(2026, 9, 26, tzinfo=UTC))
        )
        is None
    )
