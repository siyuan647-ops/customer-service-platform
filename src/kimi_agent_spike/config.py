from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_MODEL = "kimi-k2.6"
DEFAULT_TRACE_PATH = Path("artifacts/safe-trace.ndjson")


@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 45.0
    max_turns: int = 6
    trace_path: Path = DEFAULT_TRACE_PATH

    @classmethod
    def from_env(cls, *, require_api_key: bool = True) -> "Settings":
        api_key = os.getenv("KIMI_API_KEY", "").strip()
        if require_api_key and not api_key:
            raise ValueError(
                "KIMI_API_KEY is not set. Copy .env.example to .env and add the key."
            )

        timeout_seconds = float(os.getenv("AGENT_TIMEOUT_SECONDS", "45"))
        max_turns = int(os.getenv("AGENT_MAX_TURNS", "6"))
        if timeout_seconds <= 0:
            raise ValueError("AGENT_TIMEOUT_SECONDS must be greater than 0")
        if max_turns < 1:
            raise ValueError("AGENT_MAX_TURNS must be at least 1")

        return cls(
            api_key=api_key,
            base_url=os.getenv("KIMI_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            model=os.getenv("KIMI_MODEL", DEFAULT_MODEL),
            timeout_seconds=timeout_seconds,
            max_turns=max_turns,
            trace_path=Path(os.getenv("TRACE_PATH", str(DEFAULT_TRACE_PATH))),
        )
