import pytest

from kimi_agent_spike.order_service import DEMO_CUSTOMER_ID, lookup_order


@pytest.mark.asyncio
async def test_get_existing_order():
    result = await lookup_order("ord-20260918-001", DEMO_CUSTOMER_ID)
    assert result["success"] is True
    assert result["data"]["status"] == "shipped"


@pytest.mark.asyncio
async def test_get_missing_order_has_stable_error_code():
    result = await lookup_order("ORD-00000000-000", DEMO_CUSTOMER_ID)
    assert result == {
        "success": False,
        "error_code": "ORDER_NOT_FOUND",
        "message": "未找到该订单，请核对订单号。",
    }


@pytest.mark.asyncio
async def test_existing_order_is_hidden_from_non_owner():
    result = await lookup_order(
        "ORD-20260918-001", "00000000-0000-4000-8000-000000000099"
    )
    assert result == {
        "success": False,
        "error_code": "ORDER_NOT_FOUND",
        "message": "未找到该订单，请核对订单号。",
    }
