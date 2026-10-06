from __future__ import annotations

import hashlib
import re
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
    """Redact secrets and customer identifiers before writing traces or reports."""
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
