from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from backend.app.agents.contracts import AgentRequest, SupervisorPlan
from backend.app.agents.supervisor import SupervisorAgent


def test_plan_uses_safe_category_defaults_when_unknown():
    plan = SupervisorPlan(intent="general")

    assert plan.product_category is None
    assert plan.policy_category == "general"


def test_normalize_plan_preserves_llm_categories():
    plan = SupervisorPlan(
        intent="policy_query",
        product_category="食品生鲜",
        policy_category="shipping",
        need_policy_evidence=True,
    )
    request = AgentRequest(
        run_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        prompt="数码商品退款政策是什么？",
    )

    normalized = SupervisorAgent._normalize_plan(plan, request)

    assert normalized.product_category == "食品生鲜"
    assert normalized.policy_category == "shipping"


def test_normalize_plan_drops_after_sales_materials_when_order_is_available():
    plan = SupervisorPlan(
        intent="after_sales",
        order_id="ORD-20260920-003",
        need_order_data=True,
        need_policy_evidence=True,
        need_after_sales_decision=True,
        missing_information=[
            "商品问题清晰照片或视频",
            "快递面单照片",
            "商品包装整体照片",
            "问题发现的具体时间",
        ],
    )
    request = AgentRequest(
        run_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        prompt="订单 ORD-20260920-003 的车厘子破损了，我要申请退款。",
    )

    normalized = SupervisorAgent._normalize_plan(plan, request)

    assert normalized.missing_information == []


def test_normalize_plan_only_requests_order_id_before_order_workflow():
    plan = SupervisorPlan(
        intent="after_sales",
        missing_information=["商品照片", "问题发现时间"],
    )
    request = AgentRequest(
        run_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        prompt="我买的车厘子破损了，我要申请退款。",
    )

    normalized = SupervisorAgent._normalize_plan(plan, request)

    assert normalized.missing_information == ["订单号"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("product_category", "不存在的商品分类"),
        ("policy_category", "unknown_policy"),
    ],
)
def test_plan_rejects_categories_outside_candidate_set(field: str, value: str):
    payload = {"intent": "policy_query", field: value}

    with pytest.raises(ValidationError):
        SupervisorPlan.model_validate(payload)
