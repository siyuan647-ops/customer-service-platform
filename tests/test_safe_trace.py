import json

from kimi_agent_spike.safe_trace import SafeTrace, redact


def test_redact_nested_sensitive_values():
    value = redact(
        {
            "api_key": "sk-super-secret-123456",
            "arguments": {"order_id": "ORD-20260918-001"},
            "message": "联系 13800138000 或 buyer@example.com",
        }
    )
    serialized = json.dumps(value, ensure_ascii=False)
    assert "sk-super-secret" not in serialized
    assert "ORD-20260918-001" not in serialized
    assert "13800138000" not in serialized
    assert "buyer@example.com" not in serialized


def test_trace_never_writes_raw_secret(tmp_path):
    path = tmp_path / "trace.ndjson"
    SafeTrace(path).emit(
        "test",
        api_key="sk-secret-value-123456",
        order_id="ORD-20260918-001",
    )
    content = path.read_text(encoding="utf-8")
    assert "sk-secret-value" not in content
    assert "ORD-20260918-001" not in content


def test_order_id_is_redacted_inside_free_text():
    value = redact("查询订单 ORD-20260918-001 的物流")
    assert value == "查询订单 <redacted> 的物流"


def test_order_id_is_redacted_next_to_chinese_text():
    value = redact("订单ORD-20260918-001当前状态")
    assert value == "订单<redacted>当前状态"
