from __future__ import annotations

import sqlite3
import uuid

from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.agents.contracts import AfterSalesResult
from backend.app.main import create_app
from backend.app.orders.demo_data import DEMO_CUSTOMER_ID, SECONDARY_DEMO_CUSTOMER_ID


FRESH_POLICY = """【基础信息】
- 政策标题：生鲜商品售后规则
- 生效日期：2026-02-01
- 失效日期：
- 适用商品分类：生鲜食品

## 生鲜质量问题
生鲜商品签收后发现变质、破损或缺斤少两，应及时提供商品问题照片和外包装照片，可申请退款或补发。
"""

DIGITAL_POLICY = """【基础信息】
- 政策标题：数码商品售后规则
- 生效日期：2026-02-01
- 失效日期：
- 适用商品分类：数码电器

## 商品问题
数码商品签收后发现破损或故障，可提供商品问题照片申请退款或换货。
"""


def make_app(tmp_path):
    return create_app(
        Settings(
            app_env="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'after-sales-cases.db'}",
            event_backend="memory",
            minio_enabled=False,
            agent_mode="mock",
            order_backend="mock",
            embedding_mode="hash",
            auto_create_schema=True,
        )
    )


def test_case_api_is_idempotent_and_requires_evidence_before_submit(tmp_path):
    headers = {"X-Customer-ID": DEMO_CUSTOMER_ID}
    payload = {
        "order_id": "ORD-20260926-004",
        "order_item_id": "ITEM-20260926-004-01",
        "case_type": "refund",
        "reason": "收到的车厘子已经破损变质",
        "idempotency_key": "checkout-request-001",
    }
    with TestClient(make_app(tmp_path)) as client:
        first = client.post("/after-sales/cases", headers=headers, json=payload)
        second = client.post("/after-sales/cases", headers=headers, json=payload)
        third = client.post(
            "/after-sales/cases",
            headers=headers,
            json={**payload, "idempotency_key": "checkout-request-002"},
        )

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["case_no"] == second.json()["case_no"]
        assert first.json()["case_no"] == third.json()["case_no"]
        assert first.json()["status"] == "DRAFT"
        assert first.json()["evidence_required"] is True

        blocked = client.post(
            f"/after-sales/cases/{first.json()['case_no']}/submit", headers=headers
        )
        assert blocked.status_code == 422

        spoofed = client.post(
            f"/after-sales/cases/{first.json()['case_no']}/evidence",
            headers=headers,
            files={"file": ("fake.png", b"not-an-image", "image/png")},
        )
        assert spoofed.status_code == 422

        uploaded = client.post(
            f"/after-sales/cases/{first.json()['case_no']}/evidence",
            headers=headers,
            files={"file": ("damaged.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
        assert uploaded.status_code == 201
        assert uploaded.json()["analysis_status"] == "PENDING"
        assert "analysis_result" not in uploaded.json()
        assert "analysis_error" not in uploaded.json()

        still_blocked = client.post(
            f"/after-sales/cases/{first.json()['case_no']}/submit", headers=headers
        )
        assert still_blocked.status_code == 422
        assert "问题类型" in still_blocked.json()["detail"]

        details = client.patch(
            f"/after-sales/cases/{first.json()['case_no']}/materials",
            headers=headers,
            json={
                "problem_type": "运输破损",
                "problem_description": "开箱时发现部分果实破损",
            },
        )
        assert details.status_code == 200

        submitted = client.post(
            f"/after-sales/cases/{first.json()['case_no']}/submit", headers=headers
        )
        repeated = client.post(
            f"/after-sales/cases/{first.json()['case_no']}/submit", headers=headers
        )
        assert submitted.status_code == 200
        assert submitted.json()["status"] == "SUBMITTED"
        assert repeated.status_code == 200
        assert repeated.json()["status"] == "SUBMITTED"
        assert [item["action"] for item in submitted.json()["action_logs"]] == [
            "case_created",
            "evidence_uploaded",
            "materials_updated",
            "case_submitted",
        ]


def test_case_api_enforces_customer_and_order_item_ownership(tmp_path):
    payload = {
        "order_id": "ORD-20260920-003",
        "order_item_id": "ITEM-20260918-001-01",
        "case_type": "refund",
        "reason": "商品存在质量问题需要退款",
        "idempotency_key": "ownership-request-001",
    }
    with TestClient(make_app(tmp_path)) as client:
        wrong_item = client.post(
            "/after-sales/cases",
            headers={"X-Customer-ID": DEMO_CUSTOMER_ID},
            json=payload,
        )
        other_customer = client.post(
            "/after-sales/cases",
            headers={"X-Customer-ID": str(uuid.uuid4())},
            json={**payload, "idempotency_key": "ownership-request-002"},
        )

    assert wrong_item.status_code == 422
    assert other_customer.status_code == 404


def test_case_list_can_be_scoped_to_current_conversation(tmp_path):
    headers = {"X-Customer-ID": DEMO_CUSTOMER_ID}
    conversation_id = uuid.uuid4()
    payload = {
        "order_id": "ORD-20260926-004",
        "order_item_id": "ITEM-20260926-004-01",
        "case_type": "refund",
        "reason": "无线降噪耳机外壳破损",
        "idempotency_key": "conversation-filter-001",
    }
    with TestClient(make_app(tmp_path)) as client:
        created = client.post("/after-sales/cases", headers=headers, json=payload)
        all_cases = client.get("/after-sales/cases", headers=headers)
        conversation_cases = client.get(
            "/after-sales/cases",
            headers=headers,
            params={"conversation_id": str(conversation_id)},
        )

    assert created.status_code == 201
    assert len(all_cases.json()) == 1
    # A manually created case has no conversation_id and must not appear in a
    # chat window that is scoped to a specific conversation.
    assert conversation_cases.status_code == 200
    assert conversation_cases.json() == []


def test_chat_confirmation_creates_one_case_from_verified_decision(tmp_path):
    conversation_id = uuid.uuid4()
    headers = {"X-Customer-ID": DEMO_CUSTOMER_ID}
    with TestClient(make_app(tmp_path)) as client:
        uploaded = client.post(
            "/knowledge/documents",
            files={"file": ("数码商品售后规则.md", DIGITAL_POLICY.encode(), "text/markdown")},
        )
        assert uploaded.status_code == 201

        judged = client.post(
            f"/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "content": "订单 ORD-20260926-004 的无线降噪耳机破损了，我要申请退款"
            },
        )
        assert judged.status_code == 202
        conversation = client.get(
            f"/conversations/{conversation_id}", headers=headers
        ).json()
        assert "已建立待补材料的售后申请 AS-" in conversation["messages"][-1]["content"]

        confirmed = client.post(
            f"/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": "确认提交"},
        )
        assert confirmed.status_code == 202
        conversation = client.get(
            f"/conversations/{conversation_id}", headers=headers
        ).json()
        assert "正在等待材料" in conversation["messages"][-1]["content"]

        cases = client.get("/after-sales/cases", headers=headers)
        assert cases.json()[0]["status"] == "WAITING_MATERIALS"
        case_no = cases.json()[0]["case_no"]

        uploaded_evidence = client.post(
            f"/after-sales/cases/{case_no}/evidence",
            headers=headers,
            files={"file": ("damaged.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
        assert uploaded_evidence.status_code == 201
        updated_materials = client.patch(
            f"/after-sales/cases/{case_no}/materials",
            headers=headers,
            json={"problem_type": "运输破损"},
        )
        assert updated_materials.status_code == 200

        repeated = client.post(
            f"/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": "确认提交"},
        )
        assert repeated.status_code == 202
        cases = client.get("/after-sales/cases", headers=headers)

    assert cases.status_code == 200
    assert len(cases.json()) == 1
    assert cases.json()[0]["status"] == "SUBMITTED"
    assert cases.json()[0]["order_id"] == "ORD-20260926-004"


def test_missing_materials_stages_case_and_exposes_material_panel_data(tmp_path):
    class MaterialsRequiredAfterSalesAgent:
        async def decide(self, **_kwargs):
            return AfterSalesResult(
                decision="need_more_information",
                reason_code="MATERIALS_REQUIRED",
                reason="需要补充问题发生时间和商品问题凭证。",
                required_information=[
                    "商品问题的照片或视频证据",
                    "问题发现的具体时间",
                ],
                risk_level="low",
                should_handoff=False,
            )

    conversation_id = uuid.uuid4()
    headers = {"X-Customer-ID": DEMO_CUSTOMER_ID}
    with TestClient(make_app(tmp_path)) as client:
        client.app.state.responder.workflow.after_sales = MaterialsRequiredAfterSalesAgent()
        accepted = client.post(
            f"/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "content": "订单 ORD-20260926-004 的无线降噪耳机破损了，我要申请退款"
            },
        )
        assert accepted.status_code == 202

        conversation = client.get(
            f"/conversations/{conversation_id}", headers=headers
        ).json()
        cases = client.get("/after-sales/cases", headers=headers)

    assert "请在材料窗口" in conversation["messages"][-1]["content"]
    assert cases.status_code == 200
    assert len(cases.json()) == 1
    assert cases.json()[0]["status"] == "WAITING_MATERIALS"
    assert cases.json()[0]["evidence_required"] is True
    assert cases.json()[0]["problem_discovered_at_required"] is False


def test_video_evidence_is_supported(tmp_path):
    headers = {"X-Customer-ID": DEMO_CUSTOMER_ID}
    payload = {
        "order_id": "ORD-20260926-004",
        "order_item_id": "ITEM-20260926-004-01",
        "case_type": "refund",
        "reason": "商品破损，需要提供视频凭证",
        "idempotency_key": "video-request-001",
    }
    with TestClient(make_app(tmp_path)) as client:
        created = client.post("/after-sales/cases", headers=headers, json=payload)
        case_no = created.json()["case_no"]
        uploaded = client.post(
            f"/after-sales/cases/{case_no}/evidence",
            headers=headers,
            files={
                "file": (
                    "damage.mp4",
                    b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isom",
                    "video/mp4",
                )
            },
        )

    assert uploaded.status_code == 201
    assert uploaded.json()["content_type"] == "video/mp4"


def test_policy_conflict_automatically_creates_human_ticket(tmp_path):
    class ConflictAfterSalesAgent:
        async def decide(self, **_kwargs):
            return AfterSalesResult(
                decision="policy_conflict",
                reason_code="CONFLICTING_POLICY",
                reason="检索到的政策无法支持自动处理",
                risk_level="low",
                should_handoff=False,
            )

    conversation_id = uuid.uuid4()
    headers = {"X-Customer-ID": DEMO_CUSTOMER_ID}
    with TestClient(make_app(tmp_path)) as client:
        client.app.state.responder.workflow.after_sales = ConflictAfterSalesAgent()
        accepted = client.post(
            f"/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": "订单 ORD-20260926-004 的无线降噪耳机坏了，我要退款"},
        )
        assert accepted.status_code == 202
        conversation = client.get(
            f"/conversations/{conversation_id}", headers=headers
        ).json()

        assert "已自动创建人工工单" in conversation["messages"][-1]["content"]

        cases = client.get("/after-sales/cases", headers=headers)
        assert cases.status_code == 200
        assert cases.json() == []

    with sqlite3.connect(tmp_path / "after-sales-cases.db") as connection:
        assert connection.execute("SELECT count(*) FROM human_tickets").fetchone()[0] == 1


def test_paid_unshipped_order_stages_refund_without_evidence_deadline(tmp_path):
    conversation_id = uuid.uuid4()
    headers = {"X-Customer-ID": SECONDARY_DEMO_CUSTOMER_ID}
    with TestClient(make_app(tmp_path)) as client:
        accepted = client.post(
            f"/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": "订单 ORD-20260918-002 还没发货，我想退款"},
        )
        conversation = client.get(
            f"/conversations/{conversation_id}", headers=headers
        ).json()
        cases = client.get(
            "/after-sales/cases",
            headers=headers,
            params={"conversation_id": str(conversation_id)},
        ).json()

    assert accepted.status_code == 202
    assert "已付款但尚未发货" in conversation["messages"][-1]["content"]
    assert len(cases) == 1
    assert cases[0]["deadline_status"] == "not_applicable"
    assert cases[0]["evidence_required"] is False


def test_shipped_unsigned_order_routes_to_logistics_without_case(tmp_path):
    conversation_id = uuid.uuid4()
    headers = {"X-Customer-ID": DEMO_CUSTOMER_ID}
    with TestClient(make_app(tmp_path)) as client:
        accepted = client.post(
            f"/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": "订单 ORD-20260918-001 的羽绒服有问题，我要退款"},
        )
        conversation = client.get(
            f"/conversations/{conversation_id}", headers=headers
        ).json()
        cases = client.get(
            "/after-sales/cases",
            headers=headers,
            params={"conversation_id": str(conversation_id)},
        ).json()

    assert accepted.status_code == 202
    assert "已经发货但尚未签收" in conversation["messages"][-1]["content"]
    assert cases == []


def test_existing_after_sales_case_prevents_duplicate_application(tmp_path):
    conversation_id = uuid.uuid4()
    headers = {"X-Customer-ID": DEMO_CUSTOMER_ID}
    payload = {
        "order_id": "ORD-20260926-004",
        "order_item_id": "ITEM-20260926-004-01",
        "case_type": "refund",
        "reason": "无线降噪耳机外壳破损",
        "idempotency_key": "existing-case-001",
    }
    with TestClient(make_app(tmp_path)) as client:
        created = client.post("/after-sales/cases", headers=headers, json=payload)
        accepted = client.post(
            f"/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": "订单 ORD-20260926-004 的耳机破损了，我要退款"},
        )
        conversation = client.get(
            f"/conversations/{conversation_id}", headers=headers
        ).json()
        all_cases = client.get("/after-sales/cases", headers=headers).json()

    assert created.status_code == 201
    assert accepted.status_code == 202
    assert created.json()["case_no"] in conversation["messages"][-1]["content"]
    assert "无需重复申请" in conversation["messages"][-1]["content"]
    assert len(all_cases) == 1
