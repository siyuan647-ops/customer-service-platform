"""Run a bounded load test against the isolated Compose stack.

Run from the repository root with ``python -m loadtests.run``. This script only
creates accounts and conversations in the explicitly configured load-test DB.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import secrets
import subprocess
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import func, select

from backend.app.database import Database
from backend.app.models import AgentRun, Conversation, Message, OutboxEvent
from backend.app.security.provision import provision_account


DEFAULT_DATABASE_URL = "postgresql+asyncpg://loadtest:loadtest@127.0.0.1:25432/loadtest"
DEFAULT_API_URL = "http://127.0.0.1:18000"
DEFAULT_RABBIT_URL = "http://127.0.0.1:25673"
PROJECT = "csload20261006"
PROMPTS = (
    "你好，我想咨询一下购物问题。",
    "七天无理由退货政策是什么？",
    "请问商品保修政策是什么？",
    "订单 ORD-LOAD-UNKNOWN 当前是什么状态？",
)
PROMPT_TYPES = ("general", "return_policy", "warranty_policy", "unknown_order")


@dataclass
class User:
    username: str
    customer_id: uuid.UUID
    read_conversation_id: uuid.UUID
    client: httpx.AsyncClient


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 2)


def latencies(values: list[float]) -> dict[str, float | None]:
    return {
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": round(max(values), 2) if values else None,
    }


async def create_users(database: Database, api_url: str, count: int) -> list[User]:
    password = secrets.token_urlsafe(24)
    prefix = "load" + datetime.now(UTC).strftime("%m%d%H%M%S")
    users: list[User] = []
    for index in range(count):
        username = f"{prefix}{index:02d}"
        customer_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        await provision_account(database, username, customer_id, password)
        client = httpx.AsyncClient(base_url=api_url, timeout=30)
        users.append(User(username, customer_id, conversation_id, client))
    async with database.session_factory() as session:
        for user in users:
            session.add(Conversation(
                id=user.read_conversation_id,
                customer_id=user.customer_id,
                status="active",
                channel="loadtest",
            ))
        await session.flush()
        for user in users:
            for turn in range(10):
                session.add(Message(
                    conversation_id=user.read_conversation_id,
                    role="user" if turn % 2 == 0 else "assistant",
                    content=f"负载测试历史消息 {turn}",
                    status="completed",
                ))
        await session.commit()
    for user in users:
        response = await user.client.post(
            "/auth/session", json={"username": user.username, "password": password}
        )
        if response.status_code != 200:
            raise RuntimeError(f"Login failed for {user.username}: HTTP {response.status_code}")
    return users


def _memory_mib(value: str) -> float | None:
    match = re.match(r"([0-9.]+)([KMG]i?B)", value)
    if not match:
        return None
    return round(float(match.group(1)) * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024,
                                         "KB": 1 / 1000, "MB": 1000 / 1024,
                                         "GB": 1000 * 1000 / 1024}[match.group(2)], 2)


def _docker_stats(project: str) -> list[dict]:
    names = [f"{project}-{service}-1" for service in (
        "backend", "agent-worker", "after-sales-worker", "outbox-relay",
        "postgres", "redis", "rabbitmq", "minio",
    )]
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", *names],
        capture_output=True, text=True, timeout=20, check=False,
    )
    if result.returncode:
        return [{"error": result.stderr.strip()[:200]}]
    parsed = []
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
            parsed.append({
                "name": item["Name"],
                "cpu_percent": float(item["CPUPerc"].rstrip("%")),
                "memory_mib": _memory_mib(item["MemUsage"].split(" / ")[0]),
            })
        except (ValueError, KeyError, TypeError):
            continue
    return parsed


class Monitor:
    def __init__(self, database: Database, rabbit_url: str, project: str) -> None:
        self.database = database
        self.rabbit_url = rabbit_url
        self.project = project
        self.samples: list[dict] = []
        self.phase = "setup"
        self.backlog = 0
        self.last_sample_monotonic = 0.0
        self.stop = asyncio.Event()

    async def _queue(self, client: httpx.AsyncClient, name: str) -> dict:
        response = await client.get(f"/api/queues/%2F/{name}")
        if response.status_code == 404:
            return {"ready": 0, "unacked": 0}
        response.raise_for_status()
        value = response.json()
        return {
            "ready": int(value.get("messages_ready") or 0),
            "unacked": int(value.get("messages_unacknowledged") or 0),
        }

    async def _outbox_pending(self) -> int:
        async with self.database.session_factory() as session:
            query = select(func.count()).select_from(OutboxEvent).where(
                OutboxEvent.event_type == "agent.reply.requested",
                OutboxEvent.status == "PENDING",
            )
            return int(await session.scalar(query) or 0)

    async def sample(self, client: httpx.AsyncClient, index: int) -> None:
        item: dict = {"timestamp": datetime.now(UTC).isoformat(), "phase": self.phase}
        try:
            item["agent_queue"] = await self._queue(client, "agent.reply.q")
            item["dead_letter"] = await self._queue(client, "customer_service.dlq")
            item["outbox_pending"] = await self._outbox_pending()
            self.backlog = (
                item["agent_queue"]["ready"] + item["agent_queue"]["unacked"]
                + item["outbox_pending"]
            )
            item["backlog"] = self.backlog
        except Exception as exc:
            item["monitor_error"] = type(exc).__name__
        if index % 2 == 0:
            item["containers"] = await asyncio.to_thread(_docker_stats, self.project)
        self.samples.append(item)
        self.last_sample_monotonic = time.monotonic()

    async def run(self) -> None:
        async with httpx.AsyncClient(
            base_url=self.rabbit_url,
            auth=("loadtest", "loadtest"), timeout=10,
        ) as client:
            index = 0
            while not self.stop.is_set():
                await self.sample(client, index)
                index += 1
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=5)
                except TimeoutError:
                    pass


async def read_stage(users: list[User], duration: int) -> dict:
    results: list[dict] = []
    end = time.monotonic() + duration

    async def reader(user: User) -> None:
        while time.monotonic() < end:
            started = time.perf_counter()
            try:
                response = await user.client.get(f"/conversations/{user.read_conversation_id}")
                status = response.status_code
            except httpx.HTTPError:
                status = 0
            results.append({"status": status, "latency_ms": (time.perf_counter() - started) * 1000})
            await asyncio.sleep(1)

    started = time.monotonic()
    await asyncio.gather(*(reader(user) for user in users))
    elapsed = time.monotonic() - started
    return {
        "users": len(users), "duration_s": round(elapsed, 2),
        "requests": len(results), "rps": round(len(results) / elapsed, 3),
        "status_counts": dict(Counter(str(item["status"]) for item in results)),
        "latency": latencies([item["latency_ms"] for item in results]),
    }


async def _watch_reply(user: User, conversation_id: uuid.UUID) -> str:
    async with user.client.stream(
        "GET", f"/conversations/{conversation_id}/events",
        params={"cursor": "0-0"}, timeout=httpx.Timeout(30, read=None),
    ) as response:
        if response.status_code != 200:
            return f"sse_http_{response.status_code}"
        event = ""
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif not line:
                if event in {"assistant.completed", "assistant.failed"}:
                    return event
                event = ""
    return "sse_closed"


async def _send_and_watch(user: User, index: int) -> dict:
    conversation_id = uuid.uuid4()
    started = time.perf_counter()
    result: dict = {
        "conversation_id": str(conversation_id), "username": user.username,
        "prompt_type": PROMPT_TYPES[index % len(PROMPT_TYPES)],
    }
    try:
        response = await user.client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": PROMPTS[index % len(PROMPTS)]},
        )
        result["post_status"] = response.status_code
        result["post_ms"] = round((time.perf_counter() - started) * 1000, 2)
        if response.status_code != 202:
            result["result"] = f"post_http_{response.status_code}"
            return result
        result["message_id"] = response.json()["message"]["id"]
        result["result"] = await asyncio.wait_for(
            _watch_reply(user, conversation_id), timeout=240,
        )
        result["reply_ms"] = round((time.perf_counter() - started) * 1000, 2)
    except asyncio.TimeoutError:
        result["result"] = "reply_timeout"
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        result["result"] = type(exc).__name__
    return result


async def wait_for_drain(monitor: Monitor, timeout: int = 240) -> tuple[bool, float]:
    started = time.monotonic()
    consecutive_zeroes = 0
    last_checked_sample = 0.0
    while time.monotonic() - started < timeout:
        if monitor.last_sample_monotonic > max(started, last_checked_sample):
            last_checked_sample = monitor.last_sample_monotonic
            if monitor.backlog == 0:
                consecutive_zeroes += 1
                if consecutive_zeroes >= 2:
                    return True, round(time.monotonic() - started, 2)
            else:
                consecutive_zeroes = 0
        await asyncio.sleep(1)
    return False, round(time.monotonic() - started, 2)


async def message_stage(users: list[User], rate: float, duration: int, monitor: Monitor,
                        queue_stop: int) -> tuple[dict, list[dict]]:
    started = time.monotonic()
    tasks: list[asyncio.Task] = []
    stop_reason = "duration"
    count = math.floor(rate * duration)
    for index in range(count):
        target = started + index / rate
        await asyncio.sleep(max(0, target - time.monotonic()))
        if monitor.backlog > queue_stop:
            stop_reason = f"backlog_over_{queue_stop}"
            break
        tasks.append(asyncio.create_task(_send_and_watch(users[index % len(users)], index)))
    sending_elapsed = time.monotonic() - started
    monitor.phase = f"drain_{rate}"
    drained, drain_s = await wait_for_drain(monitor)
    results = await asyncio.gather(*tasks)
    post = [item["post_ms"] for item in results if "post_ms" in item]
    reply = [item["reply_ms"] for item in results if item.get("result") == "assistant.completed"]
    return ({
        "target_rps": rate, "duration_s": duration,
        "sent": len(results), "actual_send_rps": round(len(results) / sending_elapsed, 3),
        "stop_reason": stop_reason, "drained": drained, "drain_s": drain_s,
        "post_status_counts": dict(Counter(str(item.get("post_status", 0)) for item in results)),
        "result_counts": dict(Counter(item.get("result", "unknown") for item in results)),
        "post_latency": latencies(post), "reply_latency": latencies(reply),
    }, results)


async def validate_runs(database: Database, message_results: list[dict]) -> dict:
    conversation_ids = [uuid.UUID(item["conversation_id"]) for item in message_results
                        if item.get("post_status") == 202]
    if not conversation_ids:
        return {"accepted": 0, "completed": 0, "duplicate_runs": 0,
                "duplicate_assistant_messages": 0}
    async with database.session_factory() as session:
        runs = (await session.scalars(
            select(AgentRun).where(AgentRun.conversation_id.in_(conversation_ids))
        )).all()
        assistants = (await session.scalars(
            select(Message).where(Message.conversation_id.in_(conversation_ids),
                                  Message.role == "assistant")
        )).all()
    run_counts = Counter(str(item.conversation_id) for item in runs)
    answer_counts = Counter(str(item.conversation_id) for item in assistants)
    return {
        "accepted": len(conversation_ids),
        "runs": len(runs),
        "completed": sum(item.status == "completed" for item in runs),
        "failed": sum(item.status == "failed" for item in runs),
        "duplicate_runs": sum(value > 1 for value in run_counts.values()),
        "duplicate_assistant_messages": sum(value > 1 for value in answer_counts.values()),
        "missing_assistant_messages": sum(answer_counts[str(value)] == 0 for value in conversation_ids),
    }


def summarize_samples(samples: list[dict], phase: str) -> dict:
    selected = [item for item in samples if item["phase"] == phase]
    container_max: dict[str, dict] = {}
    for item in selected:
        for container in item.get("containers", []):
            name = container.get("name")
            if not name:
                continue
            peak = container_max.setdefault(name, {"cpu_percent": 0, "memory_mib": 0})
            peak["cpu_percent"] = max(peak["cpu_percent"], container.get("cpu_percent") or 0)
            peak["memory_mib"] = max(peak["memory_mib"], container.get("memory_mib") or 0)
    return {
        "samples": len(selected),
        "max_backlog": max((item.get("backlog", 0) for item in selected), default=0),
        "max_ready": max((item.get("agent_queue", {}).get("ready", 0) for item in selected), default=0),
        "max_unacked": max((item.get("agent_queue", {}).get("unacked", 0) for item in selected), default=0),
        "max_outbox_pending": max((item.get("outbox_pending", 0) for item in selected), default=0),
        "max_dead_letter": max((item.get("dead_letter", {}).get("ready", 0) for item in selected), default=0),
        "container_peaks": container_max,
    }


def write_report(report: dict, report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# Local load test report", "",
        f"- Run: `{report['run_id']}`",
        f"- Generated: {report['generated_at']}",
        f"- Mode: {report['mode']}; 1 backend, 1 agent-worker, 1 after-sales-worker",
        f"- Users: {report['users']}", "",
        "## Read stages", "",
        "| Users | Requests | RPS | HTTP status | P95 ms | P99 ms |",
        "| ---: | ---: | ---: | --- | ---: | ---: |",
    ]
    for item in report["read_stages"]:
        lines.append(f"| {item['users']} | {item['requests']} | {item['rps']} | "
                     f"{item['status_counts']} | {item['latency']['p95_ms']} | "
                     f"{item['latency']['p99_ms']} |")
    lines.extend(["", "## Message stages", "",
                  "| Target msg/s | Sent | HTTP status | Replies | POST P95 ms | Reply P50 / P95 / P99 ms | Max backlog | Drain s |",
                  "| ---: | ---: | --- | --- | ---: | ---: | ---: | ---: |"])
    for item in report["message_stages"]:
        lines.append(f"| {item['target_rps']} | {item['sent']} | {item['post_status_counts']} | "
                     f"{item['result_counts']} | {item['post_latency']['p95_ms']} | "
                     f"{item['reply_latency']['p50_ms']} / {item['reply_latency']['p95_ms']} / "
                     f"{item['reply_latency']['p99_ms']} | {item['monitor']['max_backlog']} | "
                     f"{item['drain_s']} |")
    by_type: dict[str, list[float]] = {}
    for item in report.get("message_results", []):
        if item.get("result") == "assistant.completed" and item.get("prompt_type"):
            by_type.setdefault(item["prompt_type"], []).append(item["reply_ms"])
    if by_type:
        lines.extend(["", "## Reply time by prompt type", "",
                      "| Prompt type | Count | P50 ms | P95 ms |",
                      "| --- | ---: | ---: | ---: |"])
        for name, values in sorted(by_type.items()):
            summary = latencies(values)
            lines.append(f"| {name} | {len(values)} | {summary['p50_ms']} | {summary['p95_ms']} |")
    lines.extend(["", "## Database reconciliation", "",
                  f"```json\n{json.dumps(report['database_validation'], ensure_ascii=False, indent=2)}\n```", "",
                  "## Container resource peaks", "",
                  "| Container | CPU peak (%) | Memory peak (MiB) |",
                  "| --- | ---: | ---: |"])
    peaks: dict[str, dict[str, float]] = {}
    for sample in report.get("samples", []):
        for container in sample.get("containers", []):
            name = container.get("name")
            if not name:
                continue
            peak = peaks.setdefault(name, {"cpu": 0, "memory": 0})
            peak["cpu"] = max(peak["cpu"], container.get("cpu_percent") or 0)
            peak["memory"] = max(peak["memory"], container.get("memory_mib") or 0)
    for name, peak in sorted(peaks.items()):
        lines.append(f"| {name} | {peak['cpu']:.2f} | {peak['memory']:.2f} |")
    lines.extend(["", f"Monitor errors: {report.get('monitor_errors', 0)}.",
                  "Detailed queue and resource samples are in `report.json`.", ""])
    (report_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


async def run(args: argparse.Namespace) -> None:
    database = Database(args.database_url)
    users: list[User] = []
    monitor = Monitor(database, args.rabbit_url, args.project)
    monitor_task: asyncio.Task | None = None
    report: dict = {
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": datetime.now(UTC).isoformat(),
        "users": args.users, "mode": args.mode, "project": args.project,
        "stage_duration_s": args.duration,
        "read_stages": [], "message_stages": [], "database_validation": {},
    }
    all_messages: list[dict] = []
    try:
        async with httpx.AsyncClient(base_url=args.api_url, timeout=10) as client:
            health = await client.get("/health")
            health.raise_for_status()
        users = await create_users(database, args.api_url, args.users)
        print(f"Prepared and logged in {len(users)} isolated test users", flush=True)
        monitor_task = asyncio.create_task(monitor.run())
        await asyncio.sleep(6)
        for count in args.read_users:
            monitor.phase = f"read_{count}"
            print(f"Read stage: {count} users for {args.duration}s", flush=True)
            result = await read_stage(users[:count], args.duration)
            result["monitor"] = summarize_samples(monitor.samples, monitor.phase)
            report["read_stages"].append(result)
            print(f"Read stage complete: {result['requests']} requests, p95={result['latency']['p95_ms']} ms", flush=True)
        for rate in args.rates:
            monitor.phase = f"message_{rate}"
            print(f"Message stage: {rate} msg/s for {args.duration}s", flush=True)
            result, messages = await message_stage(users, rate, args.duration, monitor, args.queue_stop)
            result["monitor"] = summarize_samples(monitor.samples, f"message_{rate}")
            report["message_stages"].append(result)
            all_messages.extend(messages)
            print(f"Message stage complete: {result['sent']} sent, {result['result_counts']}, "
                  f"peak backlog={result['monitor']['max_backlog']}", flush=True)
            if not result["drained"] or result["stop_reason"] != "duration":
                print("Stopping further stages because the queue did not remain stable", flush=True)
                break
        monitor.phase = "validation"
        report["database_validation"] = await validate_runs(database, all_messages)
        report["monitor_errors"] = sum("monitor_error" in item for item in monitor.samples)
        report["samples"] = monitor.samples
        report["message_results"] = all_messages
    finally:
        monitor.stop.set()
        if monitor_task is not None:
            await monitor_task
        for user in users:
            await user.client.aclose()
        await database.dispose()
        report["generated_at"] = datetime.now(UTC).isoformat()
        write_report(report, args.report_dir)
        print(f"Report: {args.report_dir / 'report.md'}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded local customer-service load test")
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--rabbit-url", default=DEFAULT_RABBIT_URL)
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--mode", choices=("mock", "live"), default="mock")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--duration", type=int, default=180)
    parser.add_argument("--read-users", type=lambda x: [int(v) for v in x.split(",") if v],
                        default=[5, 10, 20])
    parser.add_argument("--skip-read", action="store_true")
    parser.add_argument("--rates", type=lambda x: [float(v) for v in x.split(",")],
                        default=[0.2, 0.5, 0.8])
    parser.add_argument("--queue-stop", type=int, default=100)
    parser.add_argument("--report-dir", type=Path, default=Path("artifacts/loadtests/local"))
    args = parser.parse_args()
    if args.skip_read:
        args.read_users = []
    if args.users < max(args.read_users, default=0) or args.duration < 1 or any(rate <= 0 for rate in args.rates):
        parser.error("users must cover read stages, duration >= 1, and rates > 0")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
