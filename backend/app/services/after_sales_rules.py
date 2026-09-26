from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend.app.agents.contracts import (
    AfterSalesResult,
    OrderFacts,
    PolicyDeadlineFacts,
    PolicyEvidence,
    PolicyReference,
    ProductCategory,
)


EVIDENCE_WINDOW_HOURS_BY_CATEGORY: dict[ProductCategory, int] = {
    ProductCategory.FOOD_FRESH: 2,
    ProductCategory.CLOTHING: 48,
    ProductCategory.BEAUTY: 48,
    ProductCategory.DIGITAL: 48,
    ProductCategory.HOME_APPLIANCE: 48,
    ProductCategory.MATERNAL_CHILD: 48,
    ProductCategory.SPORTS: 48,
    ProductCategory.BOOKS: 48,
    ProductCategory.PET: 48,
    ProductCategory.HEALTH: 48,
    ProductCategory.VIRTUAL: 48,
    ProductCategory.OTHER: 48,
}

_UNPAID_PAYMENT_STATUSES = {
    "unpaid",
    "pending",
    "pending_payment",
    "failed",
    "payment_failed",
}
_REFUNDED_PAYMENT_STATUSES = {"refunded", "partially_refunded"}
_CANCELLED_ORDER_STATUSES = {"cancelled", "canceled", "closed"}
_REFUNDED_ORDER_STATUSES = {"refunded", "refund_completed"}
_PRE_SHIPMENT_ORDER_STATUSES = {
    "paid",
    "confirmed",
    "processing",
    "awaiting_shipment",
}


class AfterSalesRuleEngine:
    """Deterministic policy applicability and deadline evaluation."""

    @staticmethod
    def evaluate_order_state(order: OrderFacts) -> AfterSalesResult | None:
        """Return a deterministic decision, or None for normal signed after-sales."""
        order_status = order.status.strip().lower()
        payment_status = order.payment_status.strip().lower()

        if payment_status in _REFUNDED_PAYMENT_STATUSES or order_status in _REFUNDED_ORDER_STATUSES:
            return AfterSalesResult(
                decision="ineligible",
                reason_code="ORDER_ALREADY_REFUNDED",
                reason="订单已退款或正在退款处理中，不能重复创建售后申请。",
                recommended_actions=["请查询原退款或售后申请的处理进度"],
            )

        if payment_status in _UNPAID_PAYMENT_STATUSES:
            return AfterSalesResult(
                decision="ineligible",
                reason_code="ORDER_NOT_PAID",
                reason="订单尚未支付，没有可退资金，不能创建退款售后申请。",
                recommended_actions=["如不再需要该订单，可直接取消未付款订单"],
            )

        if order_status in _CANCELLED_ORDER_STATUSES:
            return AfterSalesResult(
                decision="ineligible",
                reason_code="ORDER_CANCELLED_OR_CLOSED",
                reason="订单已经取消或关闭，不能创建新的商品售后申请。",
                recommended_actions=["请查询订单终态或已有退款进度"],
            )

        if payment_status != "paid":
            return AfterSalesResult(
                decision="need_more_information",
                reason_code="PAYMENT_STATUS_REQUIRES_REVIEW",
                reason=f"订单支付状态 {order.payment_status} 无法自动确认售后资格。",
                recommended_actions=["由人工客服核实订单支付状态"],
                risk_level="high",
                should_handoff=True,
            )

        signed_at_values = {
            AfterSalesRuleEngine._as_utc(shipment.signed_at)
            for shipment in order.shipments
            if shipment.signed_at is not None
        }
        if signed_at_values:
            return None

        has_shipped = any(
            shipment.shipped_at is not None for shipment in order.shipments
        ) or order_status == "shipped"
        if has_shipped:
            return AfterSalesResult(
                decision="ineligible",
                reason_code="ORDER_NOT_SIGNED",
                reason="订单已经发货但尚未签收，不适用签收后的售后时效。",
                recommended_actions=["可申请物流异常处理、拒收或联系人工客服尝试拦截"],
            )

        if order_status in _PRE_SHIPMENT_ORDER_STATUSES:
            return AfterSalesResult(
                decision="eligible",
                reason_code="PRE_SHIPMENT_REFUND_ALLOWED",
                reason="订单已付款但尚未发货，可以提交取消订单或仅退款申请。",
                recommended_actions=["提交取消订单或仅退款申请"],
            )

        return AfterSalesResult(
            decision="need_more_information",
            reason_code="ORDER_STATE_REQUIRES_REVIEW",
            reason=f"订单状态 {order.status} 与当前物流信息无法支持自动售后处理。",
            recommended_actions=["由人工客服核实订单状态"],
            risk_level="high",
            should_handoff=True,
        )

    @staticmethod
    def select_applicable_evidence(
        evidence: list[PolicyEvidence],
        *,
        product_scope: str | None,
    ) -> list[PolicyEvidence]:
        # RAG evidence is only used to explain the result. Product-scoped text is
        # preferred for citations, but its wording never drives deadline logic.
        if product_scope:
            specific = [
                item
                for item in evidence
                if product_scope in item.product_categories
                and "全品类" not in item.product_categories
            ]
            if specific:
                return specific
        return evidence

    @staticmethod
    def evaluate_deadline(
        order: OrderFacts,
        *,
        product_category: ProductCategory,
        requested_at: datetime,
        evidence: list[PolicyEvidence] | None = None,
    ) -> PolicyDeadlineFacts:
        requested_at = AfterSalesRuleEngine._as_utc(requested_at)
        window_hours = EVIDENCE_WINDOW_HOURS_BY_CATEGORY[product_category]
        signed_values = {
            AfterSalesRuleEngine._as_utc(shipment.signed_at)
            for shipment in order.shipments
            if shipment.signed_at is not None
        }
        references = AfterSalesRuleEngine._deduplicate(
            [AfterSalesRuleEngine._reference(item) for item in (evidence or [])]
        )
        if len(signed_values) != 1:
            return PolicyDeadlineFacts(
                status="unknown",
                window_hours=window_hours,
                requested_at=requested_at,
                policy_references=references,
            )

        signed_at = next(iter(signed_values))
        deadline_at = signed_at + timedelta(hours=window_hours)
        remaining = int((deadline_at - requested_at).total_seconds())
        return PolicyDeadlineFacts(
            status="within_deadline" if remaining >= 0 else "expired",
            window_hours=window_hours,
            signed_at=signed_at,
            requested_at=requested_at,
            deadline_at=deadline_at,
            remaining_seconds=max(remaining, 0),
            policy_references=references,
        )

    @staticmethod
    def expired_decision(deadline: PolicyDeadlineFacts) -> AfterSalesResult:
        assert deadline.status == "expired"
        assert deadline.window_hours is not None
        assert deadline.deadline_at is not None
        return AfterSalesResult(
            decision="ineligible",
            reason_code="EVIDENCE_DEADLINE_EXPIRED",
            reason=(
                f"订单已超过签收后{deadline.window_hours}小时的举证期限，"
                f"系统计算的截止时间为{deadline.deadline_at.isoformat()}。"
            ),
            recommended_actions=[
                "如订单系统签收时间与实际收货时间不一致，可申请人工复核"
            ],
            policy_references=deadline.policy_references,
            risk_level="low",
            should_handoff=False,
        )

    @staticmethod
    def _reference(item: PolicyEvidence) -> PolicyReference:
        return PolicyReference(
            title=item.title,
            section=item.section,
            source_filename=item.source_filename,
            quote=item.content,
        )

    @staticmethod
    def _deduplicate(references: list[PolicyReference]) -> list[PolicyReference]:
        unique: list[PolicyReference] = []
        seen: set[tuple[str, str, str]] = set()
        for reference in references:
            key = (reference.source_filename, reference.section, reference.quote)
            if key not in seen:
                seen.add(key)
                unique.append(reference)
        return unique

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
