from pathlib import Path

import pytest

from kimi_agent_spike.config import Settings
import kimi_agent_spike.mock_runner as mock_runner_module
from kimi_agent_spike.mock_runner import run_mock


@pytest.mark.asyncio
async def test_mock_tool_round_trip_streams(tmp_path):
    chunks: list[str] = []
    settings = Settings(api_key="", trace_path=tmp_path / "trace.ndjson")
    outcome = await run_mock(
        "查询 ORD-20260918-001",
        settings,
        stream_writer=chunks.append,
    )
    assert outcome.tool_calls == 1
    assert "运输中" in outcome.final_output
    assert "".join(chunks) == outcome.final_output
    assert settings.trace_path.exists()


@pytest.mark.asyncio
async def test_mock_prompts_for_order_id(tmp_path):
    settings = Settings(api_key="", trace_path=Path(tmp_path) / "trace.ndjson")
    outcome = await run_mock("我的订单到哪了", settings, stream_writer=None)
    assert outcome.tool_calls == 0
    assert "订单号" in outcome.final_output


@pytest.mark.asyncio
async def test_mock_timeout_is_enforced(tmp_path, monkeypatch):
    async def slow_chunks(_text):
        await __import__("asyncio").sleep(0.05)
        yield "too late"

    monkeypatch.setattr(mock_runner_module, "_chunks", slow_chunks)
    settings = Settings(
        api_key="",
        timeout_seconds=0.001,
        trace_path=Path(tmp_path) / "trace.ndjson",
    )
    with pytest.raises(TimeoutError):
        await run_mock("查询 ORD-20260918-001", settings, stream_writer=None)
