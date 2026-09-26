from __future__ import annotations

import math
import unicodedata
from collections.abc import Sequence

from backend.app.agents.contracts import ConversationTurn


MESSAGE_OVERHEAD_TOKENS = 4


def estimate_text_tokens(value: str) -> int:
    """Conservatively estimate tokens for mixed Chinese and ASCII text.

    Kimi does not expose its tokenizer through the OpenAI-compatible SDK. Chinese
    and other non-ASCII visible characters are therefore counted one-for-one,
    while ASCII text is estimated at three characters per token.
    """
    non_ascii = 0
    ascii_characters = 0
    for character in value:
        if ord(character) < 128:
            ascii_characters += 1
        elif not unicodedata.category(character).startswith("C"):
            non_ascii += 1
    return non_ascii + math.ceil(ascii_characters / 3)


def estimate_turn_tokens(turn: ConversationTurn) -> int:
    return MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(turn.content)


def trim_history(
    turns: Sequence[ConversationTurn],
    *,
    max_tokens: int,
    max_messages: int,
) -> list[ConversationTurn]:
    """Keep the newest complete turns within both token and message limits."""
    selected: list[ConversationTurn] = []
    used_tokens = 0
    for turn in reversed(turns):
        if len(selected) >= max_messages:
            break
        turn_tokens = estimate_turn_tokens(turn)
        if used_tokens + turn_tokens > max_tokens:
            break
        selected.append(turn)
        used_tokens += turn_tokens
    selected.reverse()
    return selected
