from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "content",
    "customer_id",
    "email",
    "id_card",
    "order_id",
    "password",
    "phone",
    "prompt",
    "query",
    "secret",
    "summary",
    "token",
}
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(
        r"(?<![A-Za-z0-9])ORD-\d{8}-\d{3}(?![A-Za-z0-9])",
        re.IGNORECASE,
    ),
    re.compile(r"\b1[3-9]\d{9}\b"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)


def _fingerprint(value: Any) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]
    return f"<redacted:{digest}>"


def redact(value: Any, *, key: str | None = None) -> Any:
    if key and key.lower() in _SENSITIVE_KEYS:
        return _fingerprint(value)
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        result = value
        for pattern in _SECRET_PATTERNS:
            result = pattern.sub("<redacted>", result)
        return result
    return value


class SafeTrace:
    """A minimal local NDJSON trace that redacts secrets and customer identifiers."""

    def __init__(self, path: Path):
        self.path = path

    def emit(self, event: str, **payload: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            **redact(payload),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
