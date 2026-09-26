from __future__ import annotations

import uuid

from backend.app.agents.contracts import LogisticsFacts, ToolResult
from backend.app.orders.gateway import OrderGateway, OrderGatewayError


class OrderService:
    """Stable application service over a configurable order gateway."""

    def __init__(self, gateway: OrderGateway) -> None:
        self.gateway = gateway

    async def get_order(self, order_id: str, customer_id: uuid.UUID) -> ToolResult:
        try:
            facts = await self.gateway.get_order(order_id, customer_id)
        except OrderGatewayError:
            return ToolResult(
                success=False,
                error_code="ORDER_SOURCE_UNAVAILABLE",
                message="订单服务暂时不可用，请稍后重试。",
            )
        if facts is None:
            return ToolResult(
                success=False,
                error_code="ORDER_NOT_FOUND",
                message="未找到该订单，请核对订单号。",
            )
        return ToolResult(
            success=True,
            error_code=None,
            message="订单查询成功",
            data=facts.model_dump(mode="json"),
        )

    async def get_logistics(self, order_id: str, customer_id: uuid.UUID) -> ToolResult:
        try:
            shipments = await self.gateway.get_logistics(order_id, customer_id)
        except OrderGatewayError:
            return ToolResult(
                success=False,
                error_code="ORDER_SOURCE_UNAVAILABLE",
                message="物流服务暂时不可用，请稍后重试。",
            )
        if shipments is None:
            return ToolResult(
                success=False,
                error_code="ORDER_NOT_FOUND",
                message="未找到该订单，请核对订单号。",
            )
        logistics = LogisticsFacts(order_id=order_id.strip().upper(), shipments=shipments)
        return ToolResult(
            success=True,
            error_code=None,
            message="物流信息查询成功",
            data=logistics.model_dump(mode="json"),
        )
