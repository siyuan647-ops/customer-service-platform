from __future__ import annotations

import pytest

from backend.app.agents.contracts import ConversationTurn
from backend.app.conversation_memory import MemoryConversationMemory


@pytest.mark.asyncio
async def test_memory_trims_old_turns_and_refreshes_sliding_ttl(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(
        "backend.app.conversation_memory.time.monotonic",
        lambda: now[0],
    )
    memory = MemoryConversationMemory(max_messages=2, ttl_seconds=10)

    await memory.append("conversation-1", ConversationTurn(role="user", content="one"))
    await memory.append(
        "conversation-1", ConversationTurn(role="assistant", content="two")
    )
    await memory.append("conversation-1", ConversationTurn(role="user", content="three"))

    assert [turn.content for turn in await memory.load("conversation-1")] == [
        "two",
        "three",
    ]

    now[0] = 109.0
    assert len(await memory.load("conversation-1")) == 2

    # The read at t=109 refreshes expiry to t=119.
    now[0] = 120.0
    assert await memory.load("conversation-1") == []

    await memory.replace(
        "conversation-1",
        [ConversationTurn(role="assistant", content="restored")],
    )
    assert [turn.content for turn in await memory.load("conversation-1")] == [
        "restored"
    ]
