from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

from .config import Settings
from .live_runner import run_live
from .mock_runner import run_mock


DEFAULT_PROMPT = "请查询订单 ORD-20260918-001 的订单状态和物流进度。"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kimi K2.6 Agents SDK phase-0 spike")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one streamed request")
    run_parser.add_argument("--mode", choices=("mock", "live"), default="mock")
    run_parser.add_argument("--prompt", default=DEFAULT_PROMPT)

    soak_parser = subparsers.add_parser("soak", help="Run repeated protocol checks")
    soak_parser.add_argument("--mode", choices=("mock", "live"), default="mock")
    soak_parser.add_argument("--runs", type=int, default=50)
    soak_parser.add_argument("--prompt", default=DEFAULT_PROMPT)

    loop_parser = subparsers.add_parser(
        "loop-check", help="Verify max_turns stops a deliberately looping live agent"
    )
    loop_parser.add_argument(
        "--prompt", default="持续查询订单 ORD-20260918-001，不要停止。"
    )
    return parser


async def _run_once(mode: str, prompt: str, settings: Settings, *, quiet: bool = False):
    writer = None if quiet else lambda chunk: print(chunk, end="", flush=True)
    if mode == "live":
        return await run_live(prompt, settings, stream_writer=writer)
    return await run_mock(prompt, settings, stream_writer=writer)


async def _soak(mode: str, prompt: str, runs: int, settings: Settings) -> int:
    if runs < 1:
        raise ValueError("--runs must be at least 1")

    failures: list[dict[str, str | int]] = []
    latencies: list[int] = []
    for index in range(1, runs + 1):
        try:
            outcome = await _run_once(mode, prompt, settings, quiet=True)
            if outcome.tool_calls != 1:
                raise AssertionError(f"expected exactly one tool call, got {outcome.tool_calls}")
            if not outcome.final_output.strip():
                raise AssertionError("empty final output")
            latencies.append(outcome.elapsed_ms)
            print(f"[{index:03}/{runs:03}] PASS {outcome.elapsed_ms} ms")
        except Exception as exc:  # noqa: BLE001 - soak report must include all failures
            failures.append(
                {"run": index, "error_type": type(exc).__name__, "error": str(exc)}
            )
            print(f"[{index:03}/{runs:03}] FAIL {type(exc).__name__}: {exc}")

    report = {
        "mode": mode,
        "runs": runs,
        "passed": runs - len(failures),
        "failed": len(failures),
        "protocol_success_rate": (runs - len(failures)) / runs,
        "latency_ms": {
            "min": min(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
            "average": round(sum(latencies) / len(latencies)) if latencies else None,
        },
        "failures": failures,
    }
    report_path = Path("artifacts") / f"soak-{mode}-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failures else 0


async def _main_async(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_api_key=args.command == "loop-check" or args.mode == "live")
    if args.command == "run":
        outcome = await _run_once(args.mode, args.prompt, settings)
        print()
        print(json.dumps(asdict(outcome), ensure_ascii=False, indent=2))
        return 0
    if args.command == "soak":
        return await _soak(args.mode, args.prompt, args.runs, settings)
    if args.command == "loop-check":
        try:
            await run_live(
                args.prompt,
                settings,
                stream_writer=lambda chunk: print(chunk, end="", flush=True),
                force_tool_loop=True,
            )
        except Exception as exc:  # MaxTurnsExceeded class can vary by SDK version
            if type(exc).__name__ == "MaxTurnsExceeded":
                print(f"\nPASS: max_turns stopped the loop: {exc}")
                return 0
            raise
        print("\nFAIL: looping agent returned normally instead of reaching max_turns")
        return 1
    raise AssertionError(f"Unhandled command: {args.command}")


def main() -> None:
    load_dotenv()
    args = _parser().parse_args()
    try:
        raise SystemExit(asyncio.run(_main_async(args)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:  # concise CLI failure while keeping a useful error type
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
