from __future__ import annotations

import re


class UnsafeInputError(ValueError):
    pass


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_REQUESTS = (
    "system prompt",
    "系统提示词",
    "显示api key",
    "输出api key",
    "忽略之前的指令",
    "ignore previous instructions",
)


def validate_user_input(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise UnsafeInputError("消息不能为空")
    if _CONTROL_CHARS.search(normalized):
        raise UnsafeInputError("消息包含不允许的控制字符")
    lowered = normalized.casefold()
    if any(pattern in lowered for pattern in _SECRET_REQUESTS):
        raise UnsafeInputError("该请求涉及受保护的系统信息")
    return normalized

