from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.integrations.commerce import (
    ExternalOperationResult,
    OmsAdapter,
    PaymentAdapter,
)
from backend.app.models import (
    AfterSalesActionLog,
    AfterSalesCase,
    AfterSalesEvidence,
    AfterSalesOperation,
    AfterSalesReview,
    OutboxEvent,
)
from backend.app.orders.gateway import OrderGateway, OrderGatewayError
from backend.app.security.circuit_breaker import CircuitOpenError
from backend.app.services.after_sales_cases import (
    AfterSalesCaseConflict,
    AfterSalesCaseNotFound,
    AfterSalesCaseValidationError,
    AfterSalesDependencyError,
)
from backend.app.services.after_sales_rules import AfterSalesRuleEngine


APPROVAL_ACTIONS = {
    "cancel_and_refund",
    "refund_only",
    "return_and_refund",
    "exchange",
    "reship",
    "repair",
}
REFUND_ACTIONS = {"cancel_and_refund", "refund_only", "return_and_refund"}


class AfterSalesExecutionService:
    def __init__(
        self,
        database: Database,
        orders: OrderGateway,
        oms: OmsAdapter,
        payment: PaymentAdapter,
        settings: Settings,
    ) -> None:
        self.database = database
        self.orders = orders
        self.oms = oms
        self.payment = payment
        self.settings = settings

    @staticmethod
    def _query():
        return select(AfterSalesCase).options(
            selectinload(AfterSalesCase.evidence),
            selectinload(AfterSalesCase.action_logs),
            selectinload(AfterSalesCase.reviews),
            selectinload(AfterSalesCase.operations),
        )

    async def list_cases(self, status: str | None = None) -> list[AfterSalesCase]:
        async with self.database.session_factory() as session:
            query = self._query()
            if status:
                query = query.where(AfterSalesCase.status == status.strip().upper())
            rows = await session.scalars(query.order_by(AfterSalesCase.created_at.desc()))
            return list(rows.unique())

    async def get_case(self, case_no: str) -> AfterSalesCase:
        async with self.database.session_factory() as session:
            case = await session.scalar(
                self._query().where(
                    AfterSalesCase.case_no == case_no.strip().upper()
                )
            )
            if case is None:
                raise AfterSalesCaseNotFound("未找到该售后申请")
            return case

    async def get_order_amount(self, case: AfterSalesCase) -> tuple[Decimal, str]:
        try:
            order = await self.orders.get_order(case.order_no, case.customer_id)
        except OrderGatewayError as exc:
            raise AfterSalesDependencyError("订单系统暂时不可用") from exc
        if order is None:
            raise AfterSalesCaseNotFound("未找到售后申请对应的订单")
        return order.amount, order.currency

    async def get_evidence(self, evidence_id: uuid.UUID) -> AfterSalesEvidence:
        async with self.database.session_factory() as session:
            evidence = await session.get(AfterSalesEvidence, evidence_id)
            if evidence is None:
                raise AfterSalesCaseNotFound("未找到该售后凭证")
            return evidence

    async def start_review(self, case_no: str, reviewer_id: str) -> AfterSalesCase:
        async with self.database.session_factory() as session:
            case = await session.scalar(
                self._query()
                .where(AfterSalesCase.case_no == case_no.strip().upper())
                .with_for_update()
            )
            if case is None:
                raise AfterSalesCaseNotFound("未找到该售后申请")
            if case.status == "UNDER_REVIEW":
                return case
            if case.status != "SUBMITTED":
                raise AfterSalesCaseConflict("只有已提交的申请可以开始审核")
            previous = case.status
            case.status = "UNDER_REVIEW"
            case.action_logs.append(
                AfterSalesActionLog(
                    from_status=previous,
                    to_status=case.status,
                    action="review_started",
                    actor_type="staff",
                    actor_id=reviewer_id,
                    action_metadata={},
                )
            )
            await session.commit()
        return await self.get_case(case_no)

    async def approve(
        self,
        *,
        case_no: str,
        reviewer_id: str,
        action: str,
        refund_amount: Decimal | None,
        reason: str,
    ) -> AfterSalesCase:
        action = action.strip().lower()
        if action not in APPROVAL_ACTIONS:
            raise AfterSalesCaseValidationError("不支持的售后执行动作")
        case = await self.get_case(case_no)
        if case.status == "APPROVED" and case.resolution_action == action:
            return case
        if case.status != "UNDER_REVIEW":
            raise AfterSalesCaseConflict("申请必须先进入审核中状态")

        try:
            order = await self.orders.get_order(case.order_no, case.customer_id)
        except OrderGatewayError as exc:
            raise AfterSalesDependencyError("订单系统暂时不可用") from exc
        if order is None:
            raise AfterSalesCaseNotFound("未找到售后申请对应的订单")
        if action in REFUND_ACTIONS:
            if refund_amount is None or refund_amount <= 0 or refund_amount > order.amount:
                raise AfterSalesCaseValidationError(
                    "退款金额必须大于 0 且不能超过订单实付金额"
                )
        else:
            refund_amount = None

        state = AfterSalesRuleEngine.evaluate_order_state(order)
        if action == "cancel_and_refund":
            if state is None or state.reason_code != "PRE_SHIPMENT_REFUND_ALLOWED":
                raise AfterSalesCaseValidationError("当前订单状态不允许取消订单并退款")
        elif state is not None:
            raise AfterSalesCaseValidationError(
                f"当前订单状态不允许执行该售后动作：{state.reason}"
            )

        now = datetime.now(UTC)
        async with self.database.session_factory() as session:
            locked = await session.scalar(
                self._query()
                .where(AfterSalesCase.id == case.id)
                .with_for_update()
            )
            assert locked is not None
            if locked.status != "UNDER_REVIEW":
                raise AfterSalesCaseConflict("申请状态已变化，请刷新后重试")
            locked.status = "APPROVED"
            locked.resolution_action = action
            locked.refund_amount = refund_amount
            locked.failure_reason = None
            locked.reviewed_at = now
            locked.approved_at = now
            locked.reviews.append(
                AfterSalesReview(
                    reviewer_id=reviewer_id,
                    decision="approved",
                    action=action,
                    refund_amount=refund_amount,
                    reason=reason.strip(),
                )
            )
            locked.action_logs.append(
                AfterSalesActionLog(
                    from_status="UNDER_REVIEW",
                    to_status="APPROVED",
                    action="case_approved",
                    actor_type="staff",
                    actor_id=reviewer_id,
                    action_metadata={
                        "resolution_action": action,
                        "refund_amount": str(refund_amount),
                    },
                )
            )
            session.add(
                OutboxEvent(
                    aggregate_type="after_sales_case",
                    aggregate_id=locked.id,
                    event_type="after_sales.execute",
                    payload={"case_id": str(locked.id)},
                    idempotency_key=f"after-sales:{locked.id}:execute:v1",
                    status="PENDING",
                    attempt_count=0,
                    available_at=now,
                )
            )
            await session.commit()
        return await self.get_case(case_no)

    async def reject(
        self, *, case_no: str, reviewer_id: str, reason: str
    ) -> AfterSalesCase:
        now = datetime.now(UTC)
        async with self.database.session_factory() as session:
            case = await session.scalar(
                self._query()
                .where(AfterSalesCase.case_no == case_no.strip().upper())
                .with_for_update()
            )
            if case is None:
                raise AfterSalesCaseNotFound("未找到该售后申请")
            if case.status == "REJECTED":
                return case
            if case.status != "UNDER_REVIEW":
                raise AfterSalesCaseConflict("申请必须先进入审核中状态")
            case.status = "REJECTED"
            case.failure_reason = reason.strip()
            case.reviewed_at = now
            case.rejected_at = now
            case.reviews.append(
                AfterSalesReview(
                    reviewer_id=reviewer_id,
                    decision="rejected",
                    reason=reason.strip(),
                )
            )
            case.action_logs.append(
                AfterSalesActionLog(
                    from_status="UNDER_REVIEW",
                    to_status="REJECTED",
                    action="case_rejected",
                    actor_type="staff",
                    actor_id=reviewer_id,
                    action_metadata={"reason": reason.strip()},
                )
            )
            await session.commit()
        return await self.get_case(case_no)

    async def retry(self, case_no: str, reviewer_id: str) -> AfterSalesCase:
        now = datetime.now(UTC)
        async with self.database.session_factory() as session:
            case = await session.scalar(
                self._query()
                .where(AfterSalesCase.case_no == case_no.strip().upper())
                .with_for_update()
            )
            if case is None:
                raise AfterSalesCaseNotFound("未找到该售后申请")
            if case.status != "EXECUTION_FAILED":
                raise AfterSalesCaseConflict("只有执行失败的申请可以重试")
            event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id == case.id,
                    OutboxEvent.event_type == "after_sales.execute",
                )
                .with_for_update()
            )
            if event is None:
                raise AfterSalesCaseConflict("未找到可重试的执行任务")
            event.status = "PENDING"
            event.available_at = now
            event.locked_at = None
            event.last_error = None
            case.status = "APPROVED"
            case.failure_reason = None
            case.action_logs.append(
                AfterSalesActionLog(
                    from_status="EXECUTION_FAILED",
                    to_status="APPROVED",
                    action="execution_retried",
                    actor_type="staff",
                    actor_id=reviewer_id,
                    action_metadata={"outbox_event_id": str(event.id)},
                )
            )
            await session.commit()
        return await self.get_case(case_no)

    async def process_once(self) -> bool:
        event_id = await self._claim_event()
        if event_id is None:
            return False
        try:
            await self._process_event(event_id)
        except CircuitOpenError as exc:
            await self._defer_open_circuit(event_id, exc.retry_after_seconds)
        except Exception as exc:
            await self._mark_event_failed(event_id, exc)
        return True

    async def _defer_open_circuit(self, event_id: uuid.UUID, retry_after: int) -> None:
        async with self.database.session_factory() as session:
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if event is None:
                return
            event.status = "PENDING"
            event.available_at = datetime.now(UTC) + timedelta(seconds=max(1, retry_after))
            event.locked_at = None
            event.attempt_count = max(0, event.attempt_count - 1)
            event.last_error = "CircuitOpenError"
            case = await session.get(AfterSalesCase, event.aggregate_id, with_for_update=True)
            if case is not None and case.status == "EXECUTING":
                case.status = "APPROVED"
                case.execution_started_at = None
            await session.commit()

    async def _claim_event(self) -> uuid.UUID | None:
        now = datetime.now(UTC)
        async with self.database.session_factory() as session:
            event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type == "after_sales.execute",
                    OutboxEvent.status == "PENDING",
                    OutboxEvent.available_at <= now,
                )
                .order_by(OutboxEvent.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                return None
            event.status = "PROCESSING"
            event.locked_at = now
            event.attempt_count += 1
            await session.commit()
            return event.id

    async def _process_event(self, event_id: uuid.UUID) -> None:
        async with self.database.session_factory() as session:
            event = await session.get(OutboxEvent, event_id)
            if event is None or event.status != "PROCESSING":
                return
            case_id = uuid.UUID(str(event.payload["case_id"]))

        await self._execute_case(case_id)

        async with self.database.session_factory() as session:
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if event is not None:
                event.status = "PROCESSED"
                event.processed_at = datetime.now(UTC)
                event.locked_at = None
                event.last_error = None
                await session.commit()

    async def _execute_case(self, case_id: uuid.UUID) -> None:
        async with self.database.session_factory() as session:
            case = await session.scalar(
                self._query().where(AfterSalesCase.id == case_id).with_for_update()
            )
            if case is None:
                raise RuntimeError("After-sales case disappeared before execution")
            if case.status == "COMPLETED":
                return
            if case.status not in {"APPROVED", "EXECUTION_FAILED"}:
                raise RuntimeError(f"Case status {case.status} cannot be executed")
            previous = case.status
            case.status = "EXECUTING"
            case.execution_started_at = datetime.now(UTC)
            case.failure_reason = None
            case.action_logs.append(
                AfterSalesActionLog(
                    from_status=previous,
                    to_status="EXECUTING",
                    action="execution_started",
                    actor_type="system",
                    actor_id="after-sales-worker",
                    action_metadata={"resolution_action": case.resolution_action},
                )
            )
            action = case.resolution_action
            refund_amount = case.refund_amount
            order_no = case.order_no
            customer_id = case.customer_id
            await session.commit()

        if action is None:
            raise RuntimeError("Approved case is missing execution parameters")
        try:
            order = await self.orders.get_order(order_no, customer_id)
        except OrderGatewayError as exc:
            raise RuntimeError("Order system is unavailable during execution") from exc
        if order is None:
            raise RuntimeError("Order disappeared during execution")
        currency = order.currency

        if action == "cancel_and_refund":
            await self._set_case_status(case_id, "CANCEL_PENDING", "order_cancel_started")
            await self._run_operation(
                case_id=case_id,
                operation_type="cancel_order",
                provider=self.oms.name,
                idempotency_key=f"after-sales:{case_id}:cancel:v1",
                request_payload={"order_id": order_no},
                call=lambda: self.oms.cancel_order(
                    order_no=order_no,
                    idempotency_key=f"after-sales:{case_id}:cancel:v1",
                ),
            )

        if action in {"return_and_refund", "exchange", "reship", "repair"}:
            await self._run_operation(
                case_id=case_id,
                operation_type=f"create_{action}",
                provider=self.oms.name,
                idempotency_key=f"after-sales:{case_id}:{action}:v1",
                request_payload={"order_id": order_no, "action": action},
                call=lambda: self.oms.create_after_sales_fulfillment(
                    order_no=order_no,
                    action=action,
                    idempotency_key=f"after-sales:{case_id}:{action}:v1",
                ),
            )
            if action in {"exchange", "reship", "repair"}:
                await self._set_case_status(
                    case_id, "COMPLETED", "execution_completed"
                )
                return

        if refund_amount is None:
            raise RuntimeError("Refund action is missing refund amount")
        await self._set_case_status(case_id, "REFUND_PENDING", "refund_started")
        await self._run_operation(
            case_id=case_id,
            operation_type="refund_payment",
            provider=self.payment.name,
            idempotency_key=f"after-sales:{case_id}:refund:v1",
            request_payload={
                "order_id": order_no,
                "amount": str(refund_amount),
                "currency": currency,
            },
            call=lambda: self.payment.refund(
                order_no=order_no,
                amount=refund_amount,
                currency=currency,
                idempotency_key=f"after-sales:{case_id}:refund:v1",
            ),
        )
        await self._set_case_status(case_id, "COMPLETED", "execution_completed")

    async def _set_case_status(
        self, case_id: uuid.UUID, status: str, action: str
    ) -> None:
        async with self.database.session_factory() as session:
            case = await session.get(AfterSalesCase, case_id, with_for_update=True)
            if case is None:
                raise RuntimeError("After-sales case not found")
            previous = case.status
            case.status = status
            if status == "COMPLETED":
                case.completed_at = datetime.now(UTC)
                case.failure_reason = None
            session.add(
                AfterSalesActionLog(
                    case_id=case.id,
                    from_status=previous,
                    to_status=status,
                    action=action,
                    actor_type="system",
                    actor_id="after-sales-worker",
                    action_metadata={},
                )
            )
            await session.commit()

    async def _run_operation(
        self,
        *,
        case_id: uuid.UUID,
        operation_type: str,
        provider: str,
        idempotency_key: str,
        request_payload: dict,
        call: Callable[[], Awaitable[ExternalOperationResult]],
    ) -> AfterSalesOperation:
        async with self.database.session_factory() as session:
            operation = await session.scalar(
                select(AfterSalesOperation)
                .where(
                    AfterSalesOperation.provider == provider,
                    AfterSalesOperation.operation_type == operation_type,
                    AfterSalesOperation.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
            if operation is not None and operation.status == "SUCCEEDED":
                return operation
            if operation is None:
                operation = AfterSalesOperation(
                    case_id=case_id,
                    operation_type=operation_type,
                    provider=provider,
                    status="RUNNING",
                    idempotency_key=idempotency_key,
                    request_payload=request_payload,
                    response_payload={},
                    attempt_count=1,
                    started_at=datetime.now(UTC),
                )
                session.add(operation)
            else:
                operation.status = "RUNNING"
                operation.attempt_count += 1
                operation.started_at = datetime.now(UTC)
                operation.last_error = None
            await session.commit()
            operation_id = operation.id

        try:
            result = await call()
        except Exception as exc:
            async with self.database.session_factory() as session:
                operation = await session.get(
                    AfterSalesOperation, operation_id, with_for_update=True
                )
                assert operation is not None
                operation.status = "FAILED"
                operation.last_error = str(exc)[:2000]
                await session.commit()
            raise

        async with self.database.session_factory() as session:
            operation = await session.get(
                AfterSalesOperation, operation_id, with_for_update=True
            )
            assert operation is not None
            operation.status = "SUCCEEDED"
            operation.external_request_id = result.external_request_id
            operation.response_payload = result.payload
            operation.completed_at = datetime.now(UTC)
            operation.last_error = None
            await session.commit()
            return operation

    async def _mark_event_failed(self, event_id: uuid.UUID, exc: Exception) -> None:
        now = datetime.now(UTC)
        error = str(exc)[:2000]
        async with self.database.session_factory() as session:
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if event is None:
                return
            event.status = (
                "FAILED"
                if event.attempt_count >= self.settings.after_sales_outbox_max_attempts
                else "PENDING"
            )
            event.available_at = now + timedelta(
                seconds=min(60, 2 ** max(event.attempt_count, 1))
            )
            event.locked_at = None
            event.last_error = error
            case = await session.get(
                AfterSalesCase, event.aggregate_id, with_for_update=True
            )
            if case is not None:
                previous = case.status
                case.status = "EXECUTION_FAILED"
                case.failure_reason = error
                session.add(
                    AfterSalesActionLog(
                        case_id=case.id,
                        from_status=previous,
                        to_status="EXECUTION_FAILED",
                        action="execution_failed",
                        actor_type="system",
                        actor_id="after-sales-worker",
                        action_metadata={"error": error},
                    )
                )
            await session.commit()
