from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select

from backend.app.agents.contracts import OrderFacts
from backend.app.database import Database
from backend.app.integrations.commerce import InvoiceAdapter, OmsAdapter
from backend.app.models import CustomerOrder, CustomerServiceRequest
from backend.app.orders.gateway import OrderGateway, OrderGatewayError


RequestType = Literal["address_change", "shipment_reminder", "invoice_application"]


class CustomerOperationError(RuntimeError):
    pass


class CustomerOperationNotFound(CustomerOperationError):
    pass


class CustomerOperationConflict(CustomerOperationError):
    pass


class CustomerOperationValidationError(CustomerOperationError):
    pass


class CustomerOperationDependencyError(CustomerOperationError):
    pass


class CustomerOperationService:
    def __init__(
        self,
        database: Database,
        orders: OrderGateway,
        oms: OmsAdapter,
        invoices: InvoiceAdapter,
    ) -> None:
        self.database = database
        self.orders = orders
        self.oms = oms
        self.invoices = invoices

    @staticmethod
    def _new_request_no(request_type: RequestType) -> str:
        prefix = {
            "address_change": "ADDR",
            "shipment_reminder": "REM",
            "invoice_application": "INV",
        }[request_type]
        return f"{prefix}-{datetime.now(UTC):%Y%m%d}-{uuid.uuid4().hex[:10].upper()}"

    async def _get_order(self, customer_id: uuid.UUID, order_no: str) -> OrderFacts:
        try:
            order = await self.orders.get_order(order_no.strip().upper(), customer_id)
        except OrderGatewayError as exc:
            raise CustomerOperationDependencyError("订单系统暂时不可用") from exc
        if order is None:
            raise CustomerOperationNotFound("未找到该订单")
        return order

    @staticmethod
    def _validate_paid_unshipped(order: OrderFacts) -> None:
        if order.payment_status.lower() != "paid":
            raise CustomerOperationValidationError("只有已付款订单可以办理该操作")
        if order.shipments or order.status.lower() in {
            "shipped",
            "signed",
            "completed",
            "cancelled",
            "canceled",
            "closed",
        }:
            raise CustomerOperationValidationError("订单已经发货或结束，不能办理该操作")

    @staticmethod
    def _validate_invoice_order(order: OrderFacts) -> None:
        if order.payment_status.lower() != "paid":
            raise CustomerOperationValidationError("只有已付款订单可以申请发票")
        if order.status.lower() not in {"completed", "signed"}:
            raise CustomerOperationValidationError("订单完成后才能申请发票")
        if datetime.now(UTC) - order.created_at > timedelta(days=30):
            raise CustomerOperationValidationError("订单已超过30天补开发票期限")

    async def stage_form(
        self,
        *,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        order_no: str,
        request_type: Literal["address_change", "invoice_application"],
        idempotency_key: str,
    ) -> CustomerServiceRequest:
        order = await self._get_order(customer_id, order_no)
        if request_type == "address_change":
            self._validate_paid_unshipped(order)
        else:
            self._validate_invoice_order(order)

        async with self.database.session_factory() as session:
            existing = await session.scalar(
                select(CustomerServiceRequest).where(
                    CustomerServiceRequest.customer_id == customer_id,
                    CustomerServiceRequest.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return existing
            active = await session.scalar(
                select(CustomerServiceRequest)
                .where(
                    CustomerServiceRequest.customer_id == customer_id,
                    CustomerServiceRequest.order_no == order.order_id,
                    CustomerServiceRequest.request_type == request_type,
                    CustomerServiceRequest.status.in_(
                        ["DRAFT", "PROCESSING", "COMPLETED"]
                        if request_type == "invoice_application"
                        else ["DRAFT", "PROCESSING"]
                    ),
                )
                .order_by(CustomerServiceRequest.created_at.desc())
                .limit(1)
            )
            if active is not None:
                return active
            request = CustomerServiceRequest(
                request_no=self._new_request_no(request_type),
                conversation_id=conversation_id,
                customer_id=customer_id,
                order_no=order.order_id,
                request_type=request_type,
                status="DRAFT",
                request_payload={},
                result_payload={},
                idempotency_key=idempotency_key,
            )
            session.add(request)
            await session.commit()
            await session.refresh(request)
            return request

    async def request_shipment_reminder(
        self,
        *,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        order_no: str,
        idempotency_key: str,
    ) -> CustomerServiceRequest:
        order = await self._get_order(customer_id, order_no)
        self._validate_paid_unshipped(order)
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        async with self.database.session_factory() as session:
            duplicate = await session.scalar(
                select(CustomerServiceRequest)
                .where(
                    CustomerServiceRequest.customer_id == customer_id,
                    CustomerServiceRequest.order_no == order.order_id,
                    CustomerServiceRequest.request_type == "shipment_reminder",
                    CustomerServiceRequest.created_at >= cutoff,
                    CustomerServiceRequest.status.in_(["PROCESSING", "COMPLETED"]),
                )
                .order_by(CustomerServiceRequest.created_at.desc())
                .limit(1)
            )
            if duplicate is not None:
                return duplicate
            request = CustomerServiceRequest(
                request_no=self._new_request_no("shipment_reminder"),
                conversation_id=conversation_id,
                customer_id=customer_id,
                order_no=order.order_id,
                request_type="shipment_reminder",
                status="PROCESSING",
                request_payload={},
                result_payload={},
                idempotency_key=idempotency_key,
            )
            session.add(request)
            await session.commit()
            await session.refresh(request)

        try:
            result = await self.oms.remind_shipment(
                order_no=order.order_id,
                idempotency_key=f"shipment-reminder:{request.id}",
            )
        except Exception as exc:
            await self._finish(request.id, status="FAILED", failure_reason=type(exc).__name__)
            raise CustomerOperationDependencyError("催发货请求提交失败") from exc
        return await self._finish(
            request.id,
            status="COMPLETED",
            result_payload={
                "external_request_id": result.external_request_id,
                **result.payload,
            },
        )

    async def submit_address(
        self,
        *,
        customer_id: uuid.UUID,
        request_no: str,
        address: dict,
    ) -> CustomerServiceRequest:
        request = await self.get(customer_id, request_no)
        if request.request_type != "address_change" or request.status != "DRAFT":
            raise CustomerOperationConflict("当前申请不能提交地址")
        order = await self._get_order(customer_id, request.order_no)
        self._validate_paid_unshipped(order)

        async with self.database.session_factory() as session:
            db_order = await session.scalar(
                select(CustomerOrder).where(
                    CustomerOrder.order_no == request.order_no,
                    CustomerOrder.customer_id == customer_id,
                )
            )
            current_city = (db_order.shipping_address or {}).get("city") if db_order else None
        if current_city and address.get("city") != current_city:
            raise CustomerOperationValidationError("收货地址只能在同一城市内修改")

        await self._set_processing(request.id, address)
        try:
            result = await self.oms.update_shipping_address(
                order_no=request.order_no,
                address=address,
                idempotency_key=f"address-change:{request.id}",
            )
        except Exception as exc:
            await self._finish(request.id, status="FAILED", failure_reason=type(exc).__name__)
            raise CustomerOperationDependencyError("订单系统修改地址失败") from exc

        if db_order is not None:
            async with self.database.session_factory() as session:
                locked = await session.scalar(
                    select(CustomerOrder)
                    .where(CustomerOrder.id == db_order.id)
                    .with_for_update()
                )
                if locked is not None:
                    locked.shipping_address = dict(address)
                    await session.commit()
        return await self._finish(
            request.id,
            status="COMPLETED",
            result_payload={"external_request_id": result.external_request_id},
        )

    async def submit_invoice(
        self,
        *,
        customer_id: uuid.UUID,
        request_no: str,
        invoice_data: dict,
    ) -> CustomerServiceRequest:
        request = await self.get(customer_id, request_no)
        if request.request_type != "invoice_application" or request.status != "DRAFT":
            raise CustomerOperationConflict("当前申请不能提交开票信息")
        order = await self._get_order(customer_id, request.order_no)
        self._validate_invoice_order(order)
        if invoice_data["title_type"] == "company" and not invoice_data.get("tax_number"):
            raise CustomerOperationValidationError("企业发票必须填写纳税人识别号")
        await self._set_processing(request.id, invoice_data)
        try:
            result = await self.invoices.issue_invoice(
                order_no=request.order_no,
                invoice_data=invoice_data,
                idempotency_key=f"invoice:{request.id}",
            )
        except Exception as exc:
            await self._finish(request.id, status="FAILED", failure_reason=type(exc).__name__)
            raise CustomerOperationDependencyError("发票开具服务暂时不可用") from exc
        return await self._finish(
            request.id,
            status="COMPLETED",
            result_payload={
                "external_request_id": result.external_request_id,
                **result.payload,
            },
        )

    async def _set_processing(self, request_id: uuid.UUID, payload: dict) -> None:
        async with self.database.session_factory() as session:
            row = await session.get(CustomerServiceRequest, request_id, with_for_update=True)
            if row is None or row.status != "DRAFT":
                raise CustomerOperationConflict("申请状态已经变化")
            row.status = "PROCESSING"
            row.request_payload = dict(payload)
            await session.commit()

    async def _finish(
        self,
        request_id: uuid.UUID,
        *,
        status: str,
        result_payload: dict | None = None,
        failure_reason: str | None = None,
    ) -> CustomerServiceRequest:
        async with self.database.session_factory() as session:
            row = await session.get(CustomerServiceRequest, request_id, with_for_update=True)
            if row is None:
                raise CustomerOperationNotFound("业务申请不存在")
            row.status = status
            row.result_payload = result_payload or {}
            row.failure_reason = failure_reason
            row.completed_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            return row

    async def get(
        self, customer_id: uuid.UUID, request_no: str
    ) -> CustomerServiceRequest:
        async with self.database.session_factory() as session:
            row = await session.scalar(
                select(CustomerServiceRequest).where(
                    CustomerServiceRequest.customer_id == customer_id,
                    CustomerServiceRequest.request_no == request_no.strip().upper(),
                )
            )
            if row is None:
                raise CustomerOperationNotFound("未找到该业务申请")
            return row

    async def list(
        self,
        customer_id: uuid.UUID,
        *,
        conversation_id: uuid.UUID | None = None,
        order_no: str | None = None,
        request_type: RequestType | None = None,
    ) -> list[CustomerServiceRequest]:
        async with self.database.session_factory() as session:
            query = select(CustomerServiceRequest).where(
                CustomerServiceRequest.customer_id == customer_id
            )
            if conversation_id is not None:
                query = query.where(
                    CustomerServiceRequest.conversation_id == conversation_id
                )
            if order_no is not None:
                query = query.where(
                    CustomerServiceRequest.order_no == order_no.strip().upper()
                )
            if request_type is not None:
                query = query.where(CustomerServiceRequest.request_type == request_type)
            rows = await session.scalars(
                query.order_by(CustomerServiceRequest.created_at.desc())
            )
            return list(rows)
