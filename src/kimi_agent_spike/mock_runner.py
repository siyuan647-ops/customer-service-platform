from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator

from .config import Settings
from .live_runner import RunOutcome
from .order_service import lookup_order
from .safe_trace import SafeTrace


_ORDER_PATTERN = re.compile(r"ORD-\d{8}-\d{3}", re.IGNORECASE)


async def _chunks(text: str) -> AsyncIterator[str]:
    for index in range(0, len(text), 5):
        await asyncio.sleep(0)
        yield text[index : index + 5]


async def run_mock(prompt: str, settings: Settings, *, stream_writer=print) -> RunOutcome:
    """Deterministic offline runner for testing the harness around the real SDK."""
    started = time.perf_counter()
    trace = SafeTrace(settings.trace_path)
    trace.emit("run_start", mode="mock", prompt=prompt)

    match = _ORDER_PATTERN.search(prompt)
    if not match:
        final = "请提供完整订单号，例如 ORD-20260918-001。"
        tool_calls = 0
    else:
        tool_calls = 1
        order_id = match.group(0).upper()
        trace.emit("tool_call", tool_name="get_order", arguments={"order_id": order_id})
        result = await lookup_order(order_id)
        trace.emit("tool_result", tool_name="get_order", result=result)
        if result["success"]:
            order = result["data"]
            final = (
                f"订单{order['order_id']}当前状态为{order['status']}；"
                f"物流状态：{order['tracking_status']}。"
            )
        else:
            final = result["message"]

    async with asyncio.timeout(settings.timeout_seconds):
        async for chunk in _chunks(final):
            if stream_writer is not None:
                stream_writer(chunk)

    elapsed_ms = round((time.perf_counter() - started) * 1000)
    trace.emit("run_complete", mode="mock", tool_calls=tool_calls, final_output=final)
    return RunOutcome(final, tool_calls, elapsed_ms)

