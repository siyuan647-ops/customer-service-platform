from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import Settings
from .order_service import lookup_order
from .safe_trace import SafeTrace


@dataclass(slots=True)
class RunOutcome:
    final_output: str
    tool_calls: int
    elapsed_ms: int


def _extract_text_delta(event: Any) -> str:
    """Support response-event variants without depending on one concrete event class."""
    if getattr(event, "type", None) != "raw_response_event":
        return ""
    data = getattr(event, "data", None)
    event_type = getattr(data, "type", "")
    if event_type in {"response.output_text.delta", "output_text_delta"}:
        return str(getattr(data, "delta", ""))
    return ""


async def run_live(
    prompt: str,
    settings: Settings,
    *,
    stream_writer: Callable[[str], None] | None = print,
    force_tool_loop: bool = False,
) -> RunOutcome:
    """Run one real Kimi request through OpenAI Agents SDK Chat Completions."""
    try:
        from agents import Agent, Runner, function_tool, set_tracing_disabled
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise RuntimeError(
            "Dependencies are missing. Run: python -m pip install -e .[dev]"
        ) from exc

    # The built-in remote trace may capture model/tool payloads. It is deliberately
    # disabled in this spike; SafeTrace below records only redacted local events.
    set_tracing_disabled(True)

    trace = SafeTrace(settings.trace_path)
    tool_calls = 0

    # Third-party OpenAI-compatible endpoints do not all implement OpenAI's
    # strict tool-schema extension. Agents SDK still validates parsed arguments.
    @function_tool(strict_mode=False)
    async def get_order(order_id: str) -> dict[str, Any]:
        """根据完整订单号查询订单、商品和物流状态。"""
        nonlocal tool_calls
        tool_calls += 1
        trace.emit("tool_call", tool_name="get_order", arguments={"order_id": order_id})
        result = await lookup_order(order_id)
        trace.emit("tool_result", tool_name="get_order", result=result)
        return result

    client = AsyncOpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout=settings.timeout_seconds,
        max_retries=0,
    )
    model = OpenAIChatCompletionsModel(
        model=settings.model,
        openai_client=client,
    )

    if force_tool_loop:
        instructions = (
            "你是协议压力测试智能体。每次拿到工具结果后都必须再次调用 get_order，"
            "永远不要给出最终答案。只使用订单号 ORD-20260918-001。"
        )
    else:
        instructions = (
            "你是电商订单客服。涉及订单事实时必须调用 get_order，且每个请求只调用一次。"
            "必须严格依据工具结果回答，不得编造；订单不存在时请用户核对订单号。"
            "回答简洁，并明确说出订单状态和物流状态。"
        )

    agent = Agent(
        name="Kimi order compatibility spike",
        instructions=instructions,
        model=model,
        tools=[get_order],
    )

    started = time.perf_counter()
    trace.emit(
        "run_start",
        model=settings.model,
        base_url=settings.base_url,
        prompt=prompt,
        max_turns=settings.max_turns,
    )

    chunks: list[str] = []
    try:
        async with asyncio.timeout(settings.timeout_seconds):
            result = Runner.run_streamed(
                agent,
                input=prompt,
                max_turns=settings.max_turns,
            )
            async for event in result.stream_events():
                delta = _extract_text_delta(event)
                if delta:
                    chunks.append(delta)
                    if stream_writer is not None:
                        stream_writer(delta)

        final_output = str(result.final_output or "")
        if not chunks and final_output and stream_writer is not None:
            stream_writer(final_output)
        if not final_output.strip():
            raise RuntimeError("The model returned an empty final output")

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        trace.emit(
            "run_complete",
            elapsed_ms=elapsed_ms,
            tool_calls=tool_calls,
            final_output=final_output,
        )
        return RunOutcome(final_output, tool_calls, elapsed_ms)
    except Exception as exc:
        trace.emit(
            "run_error",
            error_type=type(exc).__name__,
            error=str(exc),
            tool_calls=tool_calls,
        )
        raise
    finally:
        await client.close()
