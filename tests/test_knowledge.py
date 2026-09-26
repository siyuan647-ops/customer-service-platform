from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.knowledge.parser import parse_document, split_document
from backend.app.main import create_app


POLICY = """【基础信息】
- 政策标题：七天无理由退货规则
- 生效日期：2026-01-01
- 失效日期：-
- 适用商品分类：全品类

1. 政策概述
商品签收次日零时起七日内，保持完好且不影响二次销售时可申请退货。

2. 特殊说明
生鲜食品不支持七天无理由退货。
"""


def make_app(tmp_path):
    return create_app(
        Settings(
            app_env="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'knowledge.db'}",
            event_backend="memory",
            minio_enabled=False,
            agent_mode="mock",
            order_backend="mock",
            embedding_mode="hash",
            auto_create_schema=True,
        )
    )


def test_parser_uses_policy_headings_for_chunks():
    parsed = parse_document(POLICY)
    chunks = split_document(parsed, max_chars=300, overlap_chars=30)
    assert parsed.title == "七天无理由退货规则"
    assert parsed.product_categories == ["全品类"]
    assert [chunk.section for chunk in chunks] == ["1. 政策概述", "2. 特殊说明"]


def test_parser_returns_multiple_product_categories():
    parsed = parse_document(
        POLICY.replace("全品类", "生鲜食品、定制商品、虚拟商品")
    )
    assert parsed.product_categories == ["生鲜食品", "定制商品", "虚拟商品"]


def test_upload_is_idempotent_and_hybrid_search_returns_citation(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        first = client.post(
            "/knowledge/documents",
            files={"file": ("七天无理由退货规则.md", POLICY.encode(), "text/markdown")},
        )
        second = client.post(
            "/knowledge/documents",
            files={"file": ("七天无理由退货规则.md", POLICY.encode(), "text/markdown")},
        )
        search = client.post(
            "/knowledge/search",
            json={"query": "生鲜可以七天无理由退货吗", "top_k": 3},
        )
        documents = client.get("/knowledge/documents")

    assert first.status_code == 201
    assert first.json()["chunk_count"] == 2
    assert first.json()["unchanged"] is False
    assert second.status_code == 201
    assert second.json()["unchanged"] is True
    assert len(documents.json()) == 1
    assert search.status_code == 200
    result = search.json()["results"][0]
    assert result["title"] == "七天无理由退货规则"
    assert result["citation"] == {
        "source_filename": "七天无理由退货规则.md",
        "title": "七天无理由退货规则",
        "section": "2. 特殊说明",
        "chunk_index": 1,
    }
    assert result["bm25_score"] > 0
    assert result["vector_rank"] is not None
    assert result["bm25_rank"] is not None


def test_upload_rejects_missing_metadata(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        response = client.post(
            "/knowledge/documents",
            files={"file": ("invalid.md", "1. 正文\n没有基础信息".encode(), "text/markdown")},
        )
    assert response.status_code == 422


def test_rule_recall_handles_chinese_number_and_synonym_normalization(tmp_path):
    logistics = """【基础信息】
- 政策标题：物流异常处理办法
- 生效日期：2026-01-01
- 失效日期：-
- 适用商品分类：全品类

1. 物流丢件
物流信息超过7天无更新，或快递公司确认丢件，视为丢件。
"""
    unrelated = POLICY.replace("七天无理由退货规则", "普通退货规则")
    with TestClient(make_app(tmp_path)) as client:
        client.post(
            "/knowledge/documents",
            files={"file": ("物流异常处理办法.md", logistics.encode(), "text/markdown")},
        )
        client.post(
            "/knowledge/documents",
            files={"file": ("普通退货规则.md", unrelated.encode(), "text/markdown")},
        )
        response = client.post(
            "/knowledge/search", json={"query": "物流七天没有更新怎么办", "top_k": 2}
        )
    assert response.status_code == 200
    assert response.json()["results"][0]["title"] == "物流异常处理办法"


def test_product_category_filter_is_exact_and_includes_global_policies(tmp_path):
    special = POLICY.replace("七天无理由退货规则", "特殊商品退款例外规则").replace(
        "全品类", "生鲜食品、定制商品"
    )
    warranty = POLICY.replace("七天无理由退货规则", "商品保修服务规范").replace(
        "全品类", "数码电器、家居家电"
    )
    with TestClient(make_app(tmp_path)) as client:
        client.post(
            "/knowledge/documents",
            files={"file": ("全品类规则.md", POLICY.encode(), "text/markdown")},
        )
        client.post(
            "/knowledge/documents",
            files={"file": ("特殊商品规则.md", special.encode(), "text/markdown")},
        )
        client.post(
            "/knowledge/documents",
            files={"file": ("保修规则.md", warranty.encode(), "text/markdown")},
        )
        response = client.post(
            "/knowledge/search",
            json={"query": "生鲜商品如何退款", "product_category": "生鲜食品", "top_k": 10},
        )

    assert response.status_code == 200
    titles = {item["title"] for item in response.json()["results"]}
    assert "七天无理由退货规则" in titles
    assert "特殊商品退款例外规则" in titles
    assert "商品保修服务规范" not in titles


def test_supervisor_policy_tool_returns_source_citation(tmp_path):
    conversation_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    with TestClient(make_app(tmp_path)) as client:
        client.post(
            "/knowledge/documents",
            files={"file": ("七天无理由退货规则.md", POLICY.encode(), "text/markdown")},
        )
        accepted = client.post(
            f"/conversations/{conversation_id}/messages",
            headers={"X-Customer-ID": str(customer_id)},
            json={"content": "生鲜商品可以七天无理由退货吗"},
        )
        conversation = client.get(
            f"/conversations/{conversation_id}",
            headers={"X-Customer-ID": str(customer_id)},
        )
    assert accepted.status_code == 202
    answer = conversation.json()["messages"][-1]["content"]
    assert "【来源：七天无理由退货规则.md，2. 特殊说明】" in answer
