from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from backend.app.after_sales_status import ACTIVE_AFTER_SALES_STATUSES
from backend.app.agents.contracts import (
    AfterSalesResult,
    OrderFacts,
    OrderItemFacts,
    PolicyDeadlineFacts,
)
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.models import (
    AfterSalesActionLog,
    AfterSalesCase,
    AfterSalesEvidence,
    OutboxEvent,
)
from backend.app.orders.gateway import OrderGateway, OrderGatewayError
from backend.app.services.after_sales_rules import AfterSalesRuleEngine
from backend.app.storage import ObjectStorage


ALLOWED_EVIDENCE_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "video/mp4",
    "video/quicktime",
}
_EVIDENCE_TERMS = ("照片", "图片", "视频", "凭证", "破损", "损坏", "变质", "少件", "质量")


def _matches_content_type(data: bytes, content_type: str) -> bool:
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if content_type in {"video/mp4", "video/quicktime"}:
        return len(data) >= 12 and data[4:8] == b"ftyp"
    return False


class AfterSalesCaseError(RuntimeError):
    pass


class AfterSalesCaseNotFound(AfterSalesCaseError):
    pass


class AfterSalesCaseConflict(AfterSalesCaseError):
    pass


class AfterSalesCaseValidationError(AfterSalesCaseError):
    pass


class AfterSalesDependencyError(AfterSalesCaseError):
    pass


class AfterSalesCaseService:
    def __init__(
        self,
        database: Database,
        orders: OrderGateway,
        storage: ObjectStorage,
        settings: Settings,
    ) -> None:
        self.database = database
        self.orders = orders
        self.storage = storage
        self.settings = settings

    @staticmethod
    def _new_case_no() -> str:
        return f"AS-{datetime.now(UTC):%Y%m%d}-{uuid.uuid4().hex[:10].upper()}"

    @staticmethod
    def _case_type_from_text(text: str) -> str:
        if "换货" in text:
            return "exchange"
        if any(term in text for term in ("补发", "少件", "漏发")):
            return "reship"
        if any(term in text for term in ("保修", "维修")):
            return "repair"
        if "退货" in text and "退款" not in text:
            return "return"
        return "refund"

    @staticmethod
    def _needs_evidence(reason: str, required_information: list[str]) -> bool:
        combined = " ".join([reason, *required_information])
        return any(term in combined for term in _EVIDENCE_TERMS)

    @staticmethod
    def _needs_problem_time(reason: str, required_information: list[str]) -> bool:
        # Deadline calculation uses the trusted order signing time. New cases do
        # not ask customers to provide a discovery time for that calculation.
        return False

    @staticmethod
    def _query():
        return select(AfterSalesCase).options(
            selectinload(AfterSalesCase.evidence),
            selectinload(AfterSalesCase.action_logs),
            selectinload(AfterSalesCase.reviews),
            selectinload(AfterSalesCase.operations),
        )

    @staticmethod
    def _missing_materials(case: AfterSalesCase) -> list[str]:
        missing: list[str] = []
        if not case.problem_type:
            missing.append("问题类型")
        if case.evidence_required and not case.evidence:
            missing.append("商品问题的照片或视频证据")
        return missing

    @staticmethod
    def _deadline_has_expired(case: AfterSalesCase) -> bool:
        if case.evidence_deadline_at is None:
            return False
        deadline = case.evidence_deadline_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return datetime.now(UTC) > deadline

    @classmethod
    def _ensure_deadline_open(cls, case: AfterSalesCase) -> None:
        if cls._deadline_has_expired(case):
            raise AfterSalesCaseValidationError(
                "该申请已超过系统计算的举证截止时间，无法继续自动提交；"
                "如签收时间有误，请申请人工复核"
            )

    async def _validate_order_item(
        self,
        *,
        customer_id: uuid.UUID,
        order_no: str,
        order_item_no: str,
    ) -> tuple[OrderFacts, OrderItemFacts]:
        try:
            order = await self.orders.get_order(order_no, customer_id)
        except OrderGatewayError as exc:
            raise AfterSalesDependencyError("订单系统暂时不可用") from exc
        if order is None:
            raise AfterSalesCaseNotFound("未找到该订单")
        order_item = next(
            (item for item in order.items if item.item_id == order_item_no),
            None,
        )
        if order_item is None:
            raise AfterSalesCaseValidationError("商品不属于该订单")
        return order, order_item

    async def _ensure_case_order_state(self, case: AfterSalesCase) -> None:
        order, _ = await self._validate_order_item(
            customer_id=case.customer_id,
            order_no=case.order_no,
            order_item_no=case.order_item_no,
        )
        current = AfterSalesRuleEngine.evaluate_order_state(order)
        if current is None:
            return
        if (
            case.reason_code == "PRE_SHIPMENT_REFUND_ALLOWED"
            and current.reason_code == "PRE_SHIPMENT_REFUND_ALLOWED"
        ):
            return
        raise AfterSalesCaseValidationError(
            f"订单状态已变化，无法继续当前申请：{current.reason}"
        )

    async def create_case(
        self,
        *,
        customer_id: uuid.UUID,
        order_no: str,
        order_item_no: str,
        case_type: str,
        reason: str,
        idempotency_key: str,
        conversation_id: uuid.UUID | None = None,
        agent_run_id: uuid.UUID | None = None,
        decision: str | None = None,
        reason_code: str | None = None,
        decision_reason: str | None = None,
        risk_level: str = "low",
        should_handoff: bool = False,
        required_information: list[str] | None = None,
        recommended_actions: list[str] | None = None,
        policy_references: list[dict] | None = None,
        evidence_deadline_at: datetime | None = None,
        deadline_window_hours: int | None = None,
        deadline_status: str | None = None,
        problem_time_required_override: bool | None = None,
        evidence_required_override: bool | None = None,
        initial_status_override: str | None = None,
    ) -> AfterSalesCase:
        order_no = order_no.strip().upper()
        order_item_no = order_item_no.strip().upper()
        idempotency_key = idempotency_key.strip()
        order, order_item = await self._validate_order_item(
            customer_id=customer_id,
            order_no=order_no,
            order_item_no=order_item_no,
        )

        state_decision = AfterSalesRuleEngine.evaluate_order_state(order)
        if state_decision is not None and state_decision.reason_code != "PRE_SHIPMENT_REFUND_ALLOWED":
            raise AfterSalesCaseValidationError(state_decision.reason)

        if state_decision is not None:
            evidence_deadline_at = None
            deadline_window_hours = None
            deadline_status = "not_applicable"
            decision = decision or state_decision.decision
            reason_code = reason_code or state_decision.reason_code
            decision_reason = decision_reason or state_decision.reason
        elif deadline_window_hours is None or deadline_status is None:
            deadline = AfterSalesRuleEngine.evaluate_deadline(
                order,
                product_category=order_item.product_category,
                requested_at=datetime.now(UTC),
            )
            evidence_deadline_at = deadline.deadline_at
            deadline_window_hours = deadline.window_hours
            deadline_status = deadline.status
            if deadline.status == "expired":
                raise AfterSalesCaseValidationError(
                    f"该商品已超过签收后{deadline.window_hours}小时的售后举证期限"
                )

        required = required_information or []
        evidence_required = self._needs_evidence(reason, required)
        if evidence_required_override is not None:
            evidence_required = evidence_required_override
        problem_time_required = self._needs_problem_time(reason, required)
        if problem_time_required_override is not None:
            problem_time_required = problem_time_required_override
        initial_status = "DRAFT"
        submitted_at = None
        if initial_status_override is not None:
            initial_status = initial_status_override
            submitted_at = None

        async with self.database.session_factory() as session:
            existing = await session.scalar(
                self._query().where(
                    AfterSalesCase.customer_id == customer_id,
                    AfterSalesCase.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return existing

            existing = await session.scalar(
                self._query()
                .where(
                    AfterSalesCase.customer_id == customer_id,
                    AfterSalesCase.order_no == order_no,
                    AfterSalesCase.order_item_no == order_item_no,
                    AfterSalesCase.status.in_(ACTIVE_AFTER_SALES_STATUSES),
                )
                .order_by(AfterSalesCase.created_at.desc())
                .limit(1)
            )
            if existing is not None:
                return existing

            case = AfterSalesCase(
                case_no=self._new_case_no(),
                conversation_id=conversation_id,
                agent_run_id=agent_run_id,
                customer_id=customer_id,
                order_no=order_no,
                order_item_no=order_item_no,
                case_type=case_type,
                reason=reason,
                status=initial_status,
                decision=decision,
                reason_code=reason_code,
                decision_reason=decision_reason,
                risk_level=risk_level,
                should_handoff=should_handoff,
                evidence_required=evidence_required,
                problem_discovered_at_required=problem_time_required,
                evidence_deadline_at=evidence_deadline_at,
                deadline_window_hours=deadline_window_hours,
                deadline_status=deadline_status,
                required_information=required,
                recommended_actions=recommended_actions or [],
                policy_references=policy_references or [],
                idempotency_key=idempotency_key,
                submitted_at=submitted_at,
            )
            case.action_logs.append(
                AfterSalesActionLog(
                    from_status=None,
                    to_status=initial_status,
                    action="case_created",
                    actor_type="agent" if agent_run_id else "customer",
                    actor_id=str(agent_run_id or customer_id),
                    action_metadata={"idempotency_key": idempotency_key},
                )
            )
            session.add(case)
            try:
                await session.commit()
            except IntegrityError:
                # A concurrent request may pass the read-side check before the
                # first transaction commits. The partial unique index is the
                # final guard; return the winning active case to keep the API
                # idempotent instead of surfacing a database error.
                await session.rollback()
                existing = await session.scalar(
                    self._query()
                    .where(
                        AfterSalesCase.customer_id == customer_id,
                        AfterSalesCase.order_no == order_no,
                        AfterSalesCase.order_item_no == order_item_no,
                        AfterSalesCase.status.in_(ACTIVE_AFTER_SALES_STATUSES),
                    )
                    .order_by(AfterSalesCase.created_at.desc())
                    .limit(1)
                )
                if existing is not None:
                    return existing
                raise
            loaded = await session.scalar(
                self._query().where(AfterSalesCase.id == case.id)
            )
            assert loaded is not None
            return loaded

    async def stage_decision(
        self,
        *,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        run_id: uuid.UUID,
        order: OrderFacts,
        order_item: OrderItemFacts,
        decision: AfterSalesResult,
        reason: str,
        policy_deadline: PolicyDeadlineFacts,
    ) -> AfterSalesCase:
        return await self.create_case(
            customer_id=customer_id,
            order_no=order.order_id,
            order_item_no=order_item.item_id,
            case_type=self._case_type_from_text(reason),
            reason=reason,
            idempotency_key=f"agent-run:{run_id}",
            conversation_id=conversation_id,
            agent_run_id=run_id,
            decision=decision.decision,
            reason_code=decision.reason_code,
            decision_reason=decision.reason,
            risk_level=decision.risk_level,
            should_handoff=decision.should_handoff,
            required_information=decision.required_information,
            recommended_actions=decision.recommended_actions,
            policy_references=[
                reference.model_dump(mode="json")
                for reference in decision.policy_references
            ],
            evidence_deadline_at=policy_deadline.deadline_at,
            deadline_window_hours=policy_deadline.window_hours,
            deadline_status=policy_deadline.status,
            problem_time_required_override=(
                False if policy_deadline.status == "within_deadline" else None
            ),
            evidence_required_override=(
                False
                if decision.reason_code == "PRE_SHIPMENT_REFUND_ALLOWED"
                else None
            ),
            initial_status_override="WAITING_MATERIALS",
        )

    async def find_existing_case(
        self,
        *,
        customer_id: uuid.UUID,
        order_no: str,
        order_item_no: str,
    ) -> AfterSalesCase | None:
        async with self.database.session_factory() as session:
            return await session.scalar(
                self._query()
                .where(
                    AfterSalesCase.customer_id == customer_id,
                    AfterSalesCase.order_no == order_no.strip().upper(),
                    AfterSalesCase.order_item_no == order_item_no.strip().upper(),
                    AfterSalesCase.status.in_(ACTIVE_AFTER_SALES_STATUSES),
                )
                .order_by(AfterSalesCase.created_at.desc())
                .limit(1)
            )

    async def confirm_latest_case(
        self,
        *,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
    ) -> AfterSalesCase:
        async with self.database.session_factory() as session:
            case = await session.scalar(
                self._query()
                .where(
                    AfterSalesCase.conversation_id == conversation_id,
                    AfterSalesCase.customer_id == customer_id,
                    AfterSalesCase.status.in_(
                        ["WAITING_MATERIALS", "DRAFT", "SUBMITTED"]
                    ),
                )
                .order_by(AfterSalesCase.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            if case is None:
                raise AfterSalesCaseValidationError("没有等待提交的售后申请")

            if case.status == "SUBMITTED":
                return case

            self._ensure_deadline_open(case)
            await self._ensure_case_order_state(case)

            previous = case.status
            next_status = "WAITING_MATERIALS" if self._missing_materials(case) else "SUBMITTED"
            if next_status == previous:
                return case
            case.status = next_status
            if next_status == "SUBMITTED":
                case.submitted_at = datetime.now(UTC)
            case.action_logs.append(
                AfterSalesActionLog(
                    from_status=previous,
                    to_status=next_status,
                    action=(
                        "case_confirmed"
                        if next_status == "SUBMITTED"
                        else "case_waiting_materials"
                    ),
                    actor_type="customer",
                    actor_id=str(customer_id),
                    action_metadata={},
                )
            )
            await session.commit()
            loaded = await session.scalar(
                self._query().where(AfterSalesCase.id == case.id)
            )
            assert loaded is not None
            return loaded

    async def list_cases(
        self,
        customer_id: uuid.UUID,
        conversation_id: uuid.UUID | None = None,
    ) -> list[AfterSalesCase]:
        async with self.database.session_factory() as session:
            query = self._query().where(AfterSalesCase.customer_id == customer_id)
            if conversation_id is not None:
                query = query.where(
                    AfterSalesCase.conversation_id == conversation_id
                )
            rows = await session.scalars(
                query.order_by(AfterSalesCase.created_at.desc())
            )
            return list(rows.unique())

    async def get_case(
        self, customer_id: uuid.UUID, case_no: str
    ) -> AfterSalesCase:
        async with self.database.session_factory() as session:
            case = await session.scalar(
                self._query().where(
                    AfterSalesCase.customer_id == customer_id,
                    AfterSalesCase.case_no == case_no.strip().upper(),
                )
            )
            if case is None:
                raise AfterSalesCaseNotFound("未找到该售后申请")
            return case

    async def add_evidence(
        self,
        *,
        customer_id: uuid.UUID,
        case_no: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> AfterSalesEvidence:
        if content_type not in ALLOWED_EVIDENCE_CONTENT_TYPES:
            raise AfterSalesCaseValidationError(
                "凭证仅支持 JPG、PNG、WebP、MP4 或 MOV"
            )
        if not _matches_content_type(data, content_type):
            raise AfterSalesCaseValidationError("凭证内容与声明的文件类型不一致")
        size_limit = (
            self.settings.after_sales_video_upload_max_bytes
            if content_type.startswith("video/")
            else self.settings.after_sales_upload_max_bytes
        )
        if len(data) > size_limit:
            raise AfterSalesCaseValidationError("凭证文件超过大小限制")
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name) or "evidence"
        async with self.database.session_factory() as session:
            case = await session.scalar(
                self._query().where(
                    AfterSalesCase.customer_id == customer_id,
                    AfterSalesCase.case_no == case_no.strip().upper(),
                )
            )
            if case is None:
                raise AfterSalesCaseNotFound("未找到该售后申请")
            self._ensure_deadline_open(case)
            await self._ensure_case_order_state(case)
            if case.status in {"APPROVED", "REJECTED", "CLOSED"}:
                raise AfterSalesCaseConflict("当前状态不能继续上传凭证")
            if len(case.evidence) >= self.settings.after_sales_max_evidence_files:
                raise AfterSalesCaseValidationError("上传凭证数量已达到上限")
            object_key = (
                f"after-sales/{customer_id}/{case.id}/{uuid.uuid4().hex}-{safe_name}"
            )
            await self.storage.put_bytes(object_key, data, content_type)
            evidence = AfterSalesEvidence(
                id=uuid.uuid4(),
                case_id=case.id,
                customer_id=customer_id,
                object_key=object_key,
                filename=Path(filename).name or safe_name,
                content_type=content_type,
                size_bytes=len(data),
                analysis_status=(
                    "PENDING"
                    if self.settings.evidence_analysis_enabled
                    else "NOT_REQUESTED"
                ),
            )
            session.add(evidence)
            if self.settings.evidence_analysis_enabled:
                session.add(
                    OutboxEvent(
                        aggregate_type="after_sales_evidence",
                        aggregate_id=evidence.id,
                        event_type="after_sales.evidence.analyze",
                        payload={
                            "evidence_id": str(evidence.id),
                            "case_id": str(case.id),
                        },
                        idempotency_key=f"evidence:{evidence.id}:analyze:v1",
                        status="PENDING",
                        attempt_count=0,
                        available_at=datetime.now(UTC),
                    )
                )
            session.add(
                AfterSalesActionLog(
                    case_id=case.id,
                    from_status=case.status,
                    to_status=case.status,
                    action="evidence_uploaded",
                    actor_type="customer",
                    actor_id=str(customer_id),
                    action_metadata={"filename": evidence.filename},
                )
            )
            await session.commit()
            await session.refresh(evidence)
            return evidence

    async def update_materials(
        self,
        *,
        customer_id: uuid.UUID,
        case_no: str,
        problem_type: str | None,
        problem_discovered_at: datetime | None,
        problem_description: str | None,
    ) -> AfterSalesCase:
        if (
            problem_type is None
            and problem_discovered_at is None
            and problem_description is None
        ):
            raise AfterSalesCaseValidationError("没有可更新的材料信息")
        if problem_type is not None and not problem_type.strip():
            raise AfterSalesCaseValidationError("问题类型不能为空")
        if problem_discovered_at is not None:
            if problem_discovered_at.tzinfo is None:
                raise AfterSalesCaseValidationError("问题发现时间必须包含时区")
            if problem_discovered_at > datetime.now(UTC):
                raise AfterSalesCaseValidationError("问题发现时间不能晚于当前时间")
        async with self.database.session_factory() as session:
            case = await session.scalar(
                self._query()
                .where(
                    AfterSalesCase.customer_id == customer_id,
                    AfterSalesCase.case_no == case_no.strip().upper(),
                )
                .with_for_update()
            )
            if case is None:
                raise AfterSalesCaseNotFound("未找到该售后申请")
            self._ensure_deadline_open(case)
            await self._ensure_case_order_state(case)
            if case.status not in {
                "DRAFT",
                "WAITING_MATERIALS",
            }:
                raise AfterSalesCaseConflict("当前状态不能修改问题材料")
            if problem_type is not None:
                case.problem_type = problem_type.strip()
            if problem_discovered_at is not None:
                case.problem_discovered_at = problem_discovered_at
            if problem_description is not None:
                case.problem_description = problem_description.strip()
            case.action_logs.append(
                AfterSalesActionLog(
                    from_status=case.status,
                    to_status=case.status,
                    action="materials_updated",
                    actor_type="customer",
                    actor_id=str(customer_id),
                    action_metadata={
                        "problem_type_updated": problem_type is not None,
                        "problem_discovered_at_updated": problem_discovered_at is not None,
                        "problem_description_updated": problem_description is not None,
                    },
                )
            )
            await session.commit()
            loaded = await session.scalar(
                self._query().where(AfterSalesCase.id == case.id)
            )
            assert loaded is not None
            return loaded

    async def submit_case(
        self, customer_id: uuid.UUID, case_no: str
    ) -> AfterSalesCase:
        async with self.database.session_factory() as session:
            case = await session.scalar(
                self._query()
                .where(
                    AfterSalesCase.customer_id == customer_id,
                    AfterSalesCase.case_no == case_no.strip().upper(),
                )
                .with_for_update()
            )
            if case is None:
                raise AfterSalesCaseNotFound("未找到该售后申请")
            self._ensure_deadline_open(case)
            if case.status == "SUBMITTED":
                return case
            await self._ensure_case_order_state(case)
            if case.status not in {
                "DRAFT",
                "WAITING_MATERIALS",
            }:
                raise AfterSalesCaseConflict("当前状态不能提交售后申请")
            missing = self._missing_materials(case)
            if missing:
                raise AfterSalesCaseValidationError(
                    "请先补充：" + "、".join(missing)
                )
            previous = case.status
            case.status = "SUBMITTED"
            case.submitted_at = datetime.now(UTC)
            case.action_logs.append(
                AfterSalesActionLog(
                    from_status=previous,
                    to_status="SUBMITTED",
                    action="case_submitted",
                    actor_type="customer",
                    actor_id=str(customer_id),
                    action_metadata={},
                )
            )
            await session.commit()
            loaded = await session.scalar(
                self._query().where(AfterSalesCase.id == case.id)
            )
            assert loaded is not None
            return loaded
