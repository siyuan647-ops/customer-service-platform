from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from backend.app.config import Settings
from kimi_agent_spike.config import Settings as SpikeSettings
from kimi_agent_spike.live_runner import run_live


class AgentResponder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        if self.settings.agent_mode == "mock":
            response = f"已收到你的问题：{prompt}。当前为工程骨架的 Mock 客服回复。"
            for index in range(0, len(response), 8):
                await asyncio.sleep(0)
                yield response[index : index + 8]
            return

        if not self.settings.kimi_api_key:
            raise RuntimeError("KIMI_API_KEY is required when AGENT_MODE=live")

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        spike_settings = SpikeSettings(
            api_key=self.settings.kimi_api_key,
            base_url=self.settings.kimi_base_url,
            model=self.settings.kimi_model,
            timeout_seconds=self.settings.agent_timeout_seconds,
            max_turns=self.settings.agent_max_turns,
            trace_path=Path(self.settings.trace_path),
        )

        async def produce() -> None:
            try:
                await run_live(prompt, spike_settings, stream_writer=queue.put_nowait)
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(produce())
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
            await task
        finally:
            if not task.done():
                task.cancel()
