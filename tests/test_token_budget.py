from backend.app.agents.contracts import ConversationTurn
from backend.app.token_budget import estimate_text_tokens, trim_history


def test_token_estimator_is_conservative_for_chinese_and_ascii() -> None:
    assert estimate_text_tokens("测试消息") == 4
    assert estimate_text_tokens("abcdef") == 2
    assert estimate_text_tokens("订单 ORD-123") >= 5


def test_history_keeps_newest_complete_messages_within_token_budget() -> None:
    turns = [
        ConversationTurn(role="user", content="甲" * 1_000),
        ConversationTurn(role="assistant", content="乙" * 1_000),
        ConversationTurn(role="user", content="丙" * 1_000),
    ]

    selected = trim_history(turns, max_tokens=2_560, max_messages=12)

    assert [turn.content[0] for turn in selected] == ["乙", "丙"]
    assert all(len(turn.content) == 1_000 for turn in selected)


def test_history_also_enforces_message_count() -> None:
    turns = [
        ConversationTurn(role="user", content=f"message-{index}")
        for index in range(15)
    ]

    selected = trim_history(turns, max_tokens=2_560, max_messages=12)

    assert len(selected) == 12
    assert selected[0].content == "message-3"
    assert selected[-1].content == "message-14"


def test_oversized_latest_history_message_is_not_partially_truncated() -> None:
    turns = [ConversationTurn(role="user", content="超" * 3_000)]

    assert trim_history(turns, max_tokens=2_560, max_messages=12) == []
