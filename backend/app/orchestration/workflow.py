from __future__ import annotations

from backend.app.agents.after_sales import AfterSalesAgent
from backend.app.agents.contracts import (
    AfterSalesCaseFacts,
    AfterSalesResult,
    CustomerOperationFacts,
    LogisticsFacts,
    OrderFacts,
    OrderItemFacts,
    PolicyDeadlineFacts,
    PolicyEvidence,
    ProductCategory,
    ProductTag,
    SupervisorPlan,
    WorkflowResult,
)
from backend.app.agents.tools import ToolRuntime
from backend.app.services.after_sales_rules import AfterSalesRuleEngine


_CATEGORY_POLICY_SCOPE = {
    ProductCategory.FOOD_FRESH: ProductTag.FRESH.value,
}

class CustomerServiceWorkflow:
    def __init__(self, after_sales: AfterSalesAgent) -> None:
        self.after_sales = after_sales

    @staticmethod
    def _transition(runtime: ToolRuntime, state: str) -> None:
        runtime.recorder.emit("workflow_state_changed", state=state)

    @staticmethod
    def _evidence(items: list[dict]) -> list[PolicyEvidence]:
        return [
            PolicyEvidence(
                document_id=item["document_id"],
                chunk_id=item["chunk_id"],
                title=item["title"],
                section=item["section"],
                content=item["content"],
                product_categories=item["product_categories"],
                source_filename=item["citation"]["source_filename"],
                score=item["score"],
            )
            for item in items
        ]

    @staticmethod
    def _policy_scope(
        product_category: ProductCategory | None,
        product_tags: list[ProductTag],
    ) -> str | None:
        if product_tags:
            return product_tags[0].value
        if product_category is None:
            return None
        return _CATEGORY_POLICY_SCOPE.get(product_category, product_category.value)

    @staticmethod
    def _select_order_item(
        order: OrderFacts,
        plan: SupervisorPlan,
        prompt: str,
    ) -> OrderItemFacts | None:
        if plan.order_item_id:
            return next(
                (item for item in order.items if item.item_id == plan.order_item_id),
                None,
            )
        mentioned = [item for item in order.items if item.product_name in prompt]
        if len(mentioned) == 1:
            return mentioned[0]
        if len(order.items) == 1:
            return order.items[0]
        return None

    @staticmethod
    def _validate_after_sales(
        decision: AfterSalesResult, evidence: list[PolicyEvidence]
    ) -> AfterSalesResult:
        if decision.decision not in {"eligible", "ineligible"}:
            return decision
        allowed = {
            (item.title, item.section, item.source_filename, item.content)
            for item in evidence
        }
        references_are_valid = bool(decision.policy_references) and all(
            (ref.title, ref.section, ref.source_filename, ref.quote) in allowed
            for ref in decision.policy_references
        )
        if references_are_valid:
            return decision
        return AfterSalesResult(
            decision="policy_conflict",
            reason_code="UNVERIFIED_POLICY_REFERENCE",
            reason="售后结论未能通过政策引用校验。",
            required_information=[],
            recommended_actions=["转人工客服复核政策依据"],
            policy_references=[],
            risk_level="high",
            should_handoff=True,
        )

    @staticmethod
    def _case_facts(case) -> AfterSalesCaseFacts:
        return AfterSalesCaseFacts(
            case_no=case.case_no,
            order_id=case.order_no,
            order_item_id=case.order_item_no,
            case_type=case.case_type,
            status=case.status,
            evidence_required=case.evidence_required,
            problem_discovered_at_required=case.problem_discovered_at_required,
            evidence_deadline_at=case.evidence_deadline_at,
            deadline_status=case.deadline_status,
        )

    @staticmethod
    def _operation_facts(data: dict) -> CustomerOperationFacts:
        return CustomerOperationFacts.model_validate(data)

    async def _finalize_after_sales(
        self,
        *,
        plan: SupervisorPlan,
        prompt: str,
        runtime: ToolRuntime,
        order: OrderFacts,
        order_item: OrderItemFacts,
        evidence: list[PolicyEvidence],
        policy_deadline,
        decision: AfterSalesResult,
        warnings: list[str],
        validate_policy_references: bool,
    ) -> WorkflowResult:
        self._transition(runtime, "RESULT_VALIDATION")
        validated = (
            self._validate_after_sales(decision, evidence)
            if validate_policy_references
            else decision
        )
        if validated.decision == "policy_conflict":
            validated = validated.model_copy(
                update={"risk_level": "high", "should_handoff": True}
            )
        elif validated.risk_level == "high" and not validated.should_handoff:
            validated = validated.model_copy(update={"should_handoff": True})

        case_facts = None
        # Opening the material form is an intake action, not a refund approval.
        # Once order/item/policy checks have passed, both an eligible result and
        # user-supplied details still needed by the case can be collected there.
        # Natural-language wording in required_information must not control flow.
        should_stage_case = validated.decision in {"eligible", "need_more_information"}
        if should_stage_case and not validated.should_handoff:
            pending_case = await runtime.after_sales_cases.stage_decision(
                conversation_id=runtime.conversation_id,
                customer_id=runtime.customer_id,
                run_id=runtime.run_id,
                order=order,
                order_item=order_item,
                decision=validated,
                reason=prompt,
                policy_deadline=policy_deadline,
            )
            runtime.recorder.emit(
                "after_sales_case_staged",
                case_no=pending_case.case_no,
                status=pending_case.status,
            )
            case_facts = self._case_facts(pending_case)

        ticket_id = None
        if validated.should_handoff:
            self._transition(runtime, "HANDOFF")
            ticket_result = await runtime.execute(
                "create_human_ticket",
                {
                    "reason": "other",
                    "summary": (
                        f"订单 {order.order_id} 商品 {order_item.item_id} 售后需要人工处理："
                        f"{validated.reason}"
                    )[:500],
                    "priority": "high" if validated.risk_level == "high" else "normal",
                },
            )
            if ticket_result["success"]:
                ticket_id = ticket_result["data"]["ticket_id"]

        status = (
            "handoff_created"
            if ticket_id
            else "need_more_information"
            if validated.decision == "need_more_information"
            else "success"
        )
        return WorkflowResult(
            status=status,
            plan=plan,
            order=order,
            order_item=order_item,
            evidence=evidence,
            policy_deadline=policy_deadline,
            after_sales=validated,
            after_sales_case=case_facts,
            ticket_id=ticket_id,
            required_information=validated.required_information,
            warnings=warnings,
        )

    async def execute(
        self,
        *,
        plan: SupervisorPlan,
        prompt: str,
        runtime: ToolRuntime,
    ) -> WorkflowResult:
        self._transition(runtime, "ROUTED")

        if plan.missing_information:
            self._transition(runtime, "WAITING_USER")
            return WorkflowResult(
                status="need_more_information",
                plan=plan,
                required_information=plan.missing_information,
            )

        if plan.intent == "human_handoff":
            self._transition(runtime, "HANDOFF")
            ticket_summary = (
                f"用户主动请求转接人工客服。用户原话：{prompt.strip()}"
            )[:500]
            result = await runtime.execute(
                "create_human_ticket",
                {
                    "reason": "other",
                    "summary": ticket_summary,
                    "priority": "normal",
                },
            )
            if not result["success"]:
                return WorkflowResult(
                    status="error",
                    plan=plan,
                    message=result["message"],
                )
            return WorkflowResult(
                status="handoff_created",
                plan=plan,
                ticket_id=result["data"]["ticket_id"],
            )

        if plan.intent == "after_sales_confirm":
            self._transition(runtime, "AFTER_SALES_SUBMIT")
            result = await runtime.execute(
                "create_after_sales_case", {"confirmed": True}
            )
            if not result["success"]:
                return WorkflowResult(
                    status="error", plan=plan, message=result["message"]
                )
            return WorkflowResult(
                status="success",
                plan=plan,
                after_sales_case=AfterSalesCaseFacts.model_validate(result["data"]),
            )

        if plan.intent == "order_query":
            self._transition(runtime, "ORDER_LOOKUP")
            result = await runtime.execute("get_order", {"order_id": plan.order_id})
            if not result["success"]:
                return WorkflowResult(status="not_found", plan=plan, message=result["message"])
            return WorkflowResult(
                status="success", plan=plan, order=OrderFacts.model_validate(result["data"])
            )

        if plan.intent == "logistics_query":
            self._transition(runtime, "LOGISTICS_LOOKUP")
            result = await runtime.execute("get_logistics", {"order_id": plan.order_id})
            if not result["success"]:
                return WorkflowResult(status="not_found", plan=plan, message=result["message"])
            return WorkflowResult(
                status="success",
                plan=plan,
                logistics=LogisticsFacts.model_validate(result["data"]),
            )

        if plan.intent == "address_change":
            self._transition(runtime, "ADDRESS_CHANGE_FORM")
            result = await runtime.execute(
                "stage_address_change", {"order_id": plan.order_id}
            )
            return WorkflowResult(
                status="success" if result["success"] else "error",
                plan=plan,
                customer_operation=(
                    self._operation_facts(result["data"]) if result["success"] else None
                ),
                message=None if result["success"] else result["message"],
            )

        if plan.intent == "shipment_reminder":
            self._transition(runtime, "SHIPMENT_REMINDER")
            result = await runtime.execute(
                "create_shipment_reminder", {"order_id": plan.order_id}
            )
            return WorkflowResult(
                status="success" if result["success"] else "error",
                plan=plan,
                customer_operation=(
                    self._operation_facts(result["data"]) if result["success"] else None
                ),
                message=None if result["success"] else result["message"],
            )

        if plan.intent == "invoice_apply":
            self._transition(runtime, "INVOICE_FORM")
            result = await runtime.execute(
                "stage_invoice_application", {"order_id": plan.order_id}
            )
            return WorkflowResult(
                status="success" if result["success"] else "error",
                plan=plan,
                customer_operation=(
                    self._operation_facts(result["data"]) if result["success"] else None
                ),
                message=None if result["success"] else result["message"],
            )

        if plan.intent == "invoice_query":
            self._transition(runtime, "INVOICE_LOOKUP")
            result = await runtime.execute("get_invoice", {"order_id": plan.order_id})
            return WorkflowResult(
                status="success" if result["success"] else "not_found",
                plan=plan,
                customer_operation=(
                    self._operation_facts(result["data"]) if result["success"] else None
                ),
                message=None if result["success"] else result["message"],
            )

        if plan.intent == "after_sales_status":
            self._transition(runtime, "AFTER_SALES_STATUS_LOOKUP")
            result = await runtime.execute(
                "get_after_sales_status", {"order_id": plan.order_id}
            )
            return WorkflowResult(
                status="success" if result["success"] else "not_found",
                plan=plan,
                after_sales_case=(
                    AfterSalesCaseFacts.model_validate(result["data"])
                    if result["success"]
                    else None
                ),
                message=None if result["success"] else result["message"],
            )

        if plan.intent == "policy_query":
            self._transition(runtime, "POLICY_RETRIEVAL")
            result = await runtime.execute(
                "search_policy",
                {
                    "query": prompt,
                    "category": plan.policy_category,
                    "product_category": self._policy_scope(
                        plan.product_category, plan.product_tags
                    ),
                    "top_k": 5,
                },
            )
            evidence = self._evidence(result["data"]["items"])
            return WorkflowResult(
                status="success" if evidence else "not_found",
                plan=plan,
                evidence=evidence,
                message=None if evidence else "没有检索到匹配政策。",
            )

        if plan.intent == "after_sales":
            return await self._execute_after_sales(plan=plan, prompt=prompt, runtime=runtime)

        self._transition(runtime, "GENERAL_RESPONSE")
        return WorkflowResult(status="success", plan=plan)

    async def _execute_after_sales(
        self,
        *,
        plan: SupervisorPlan,
        prompt: str,
        runtime: ToolRuntime,
    ) -> WorkflowResult:
        self._transition(runtime, "ORDER_LOOKUP")
        order_result = await runtime.execute("get_order", {"order_id": plan.order_id})
        if not order_result["success"]:
            return WorkflowResult(
                status="not_found", plan=plan, message=order_result["message"]
            )
        order = OrderFacts.model_validate(order_result["data"])

        order_item = self._select_order_item(order, plan, prompt)
        if order_item is None:
            self._transition(runtime, "WAITING_USER")
            choices = "、".join(
                f"{item.product_name}（{item.item_id}）" for item in order.items
            )
            return WorkflowResult(
                status="need_more_information",
                plan=plan,
                order=order,
                required_information=[f"需要售后的商品：{choices}"],
            )

        await runtime.conversation_context.set_active_order(
            conversation_id=runtime.conversation_id,
            customer_id=runtime.customer_id,
            order_id=order.order_id,
            order_item_id=order_item.item_id,
        )

        warnings: list[str] = []
        if plan.product_category and plan.product_category != order_item.product_category:
            warnings.append("用户描述的商品分类与订单商品分类不一致，已使用订单商品分类。")
        product_scope = self._policy_scope(
            order_item.product_category, order_item.product_tags
        )

        existing_case = await runtime.after_sales_cases.find_existing_case(
            customer_id=runtime.customer_id,
            order_no=order.order_id,
            order_item_no=order_item.item_id,
        )
        if existing_case is not None:
            runtime.recorder.emit(
                "deterministic_rule_applied",
                rule="existing_after_sales_case",
                case_no=existing_case.case_no,
                status=existing_case.status,
            )
            return WorkflowResult(
                status="success",
                plan=plan,
                order=order,
                order_item=order_item,
                after_sales=AfterSalesResult(
                    decision="ineligible",
                    reason_code="AFTER_SALES_CASE_EXISTS",
                    reason=(
                        f"该订单商品已有售后申请 {existing_case.case_no}，"
                        f"当前状态为 {existing_case.status}，无需重复申请。"
                    ),
                    recommended_actions=["继续补充原申请材料或查询原申请进度"],
                ),
                after_sales_case=self._case_facts(existing_case),
                warnings=warnings,
            )

        order_state_decision = AfterSalesRuleEngine.evaluate_order_state(order)
        if order_state_decision is not None:
            policy_deadline = PolicyDeadlineFacts(
                status="not_applicable",
                requested_at=runtime.started_at,
            )
            runtime.recorder.emit(
                "deterministic_rule_applied",
                rule="order_state",
                order_status=order.status,
                payment_status=order.payment_status,
                decision=order_state_decision.model_dump(mode="json"),
            )
            return await self._finalize_after_sales(
                plan=plan,
                prompt=prompt,
                runtime=runtime,
                order=order,
                order_item=order_item,
                evidence=[],
                policy_deadline=policy_deadline,
                decision=order_state_decision,
                warnings=warnings,
                validate_policy_references=False,
            )

        self._transition(runtime, "POLICY_RETRIEVAL")
        policy_result = await runtime.execute(
            "search_policy",
            {
                "query": prompt,
                "category": plan.policy_category,
                "product_category": product_scope,
                "top_k": 5,
            },
        )
        retrieved_evidence = self._evidence(policy_result["data"]["items"])
        evidence = AfterSalesRuleEngine.select_applicable_evidence(
            retrieved_evidence,
            product_scope=product_scope,
        )
        policy_deadline = AfterSalesRuleEngine.evaluate_deadline(
            order,
            product_category=order_item.product_category,
            requested_at=runtime.started_at,
            evidence=evidence,
        )
        runtime.recorder.emit(
            "after_sales_deadline_evaluated",
            deadline=policy_deadline.model_dump(mode="json"),
            retrieved_evidence_count=len(retrieved_evidence),
            applicable_evidence_count=len(evidence),
        )

        self._transition(runtime, "AFTER_SALES_DECISION")
        if policy_deadline.status == "expired":
            decision = AfterSalesRuleEngine.expired_decision(policy_deadline)
            runtime.recorder.emit(
                "deterministic_rule_applied",
                rule="evidence_deadline",
                decision=decision.model_dump(mode="json"),
            )
        else:
            runtime.recorder.emit("expert_agent_started", agent="after_sales_agent")
            decision = await self.after_sales.decide(
                user_request=prompt,
                order=order,
                order_item=order_item,
                evidence=evidence,
                policy_deadline=policy_deadline,
            )
            runtime.recorder.emit(
                "expert_agent_completed",
                agent="after_sales_agent",
                decision=decision.model_dump(mode="json"),
            )

        return await self._finalize_after_sales(
            plan=plan,
            prompt=prompt,
            runtime=runtime,
            order=order,
            order_item=order_item,
            evidence=evidence,
            policy_deadline=policy_deadline,
            decision=decision,
            warnings=warnings,
            validate_policy_references=True,
        )
