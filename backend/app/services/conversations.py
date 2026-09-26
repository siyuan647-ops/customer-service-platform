from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from backend.app.database import Database
from backend.app.conversation_memory import ConversationMemory
from backend.app.events import EventBroker
from backend.app.agents.contracts import AgentRequest, ConversationTurn
from backend.app.agents.supervisor import SupervisorAgent
from backend.app.models import AgentRun, Conversation, Message
from backend.app.token_budget import trim_history


logger = structlog.get_logger()


async def process_assistant_reply(
    *,
    database: Database,
    broker: EventBroker,
    memory: ConversationMemory,
    responder: SupervisorAgent,
    conversation_id: uuid.UUID,
    customer_id: uuid.UUID,
    input_message_id: uuid.UUID,
    prompt: str,
) -> None:
    run_uuid = uuid.uuid4()
    run_id = str(run_uuid)
    async with database.session_factory() as session:
        session.add(
            AgentRun(
                id=run_uuid,
                conversation_id=conversation_id,
                input_message_id=input_message_id,
                status="running",
                model=(
                    responder.settings.kimi_model
                    if responder.settings.agent_mode == "live"
                    else "mock"
                ),
                trace=[],
            )
        )
        await session.commit()
    await broker.publish(str(conversation_id), "assistant.started", {"run_id": run_id})
    chunks: list[str] = []
    try:
        async def publish_delta(chunk: str) -> None:
            chunks.append(chunk)
            await broker.publish(
                str(conversation_id), "assistant.delta", {"run_id": run_id, "delta": chunk}
            )

        try:
            history = await memory.load(str(conversation_id))
        except Exception:
            logger.warning(
                "conversation_memory_read_failed",
                conversation_id=str(conversation_id),
            )
            history = []

        if not history:
            history_rows = await list_recent_messages(
                database,
                conversation_id,
                exclude_message_id=input_message_id,
                limit=responder.settings.agent_history_messages,
            )
            history = [
                ConversationTurn(role=message.role, content=message.content)
                for message in history_rows
            ]
            if history:
                try:
                    await memory.replace(str(conversation_id), history)
                except Exception:
                    logger.warning(
                        "conversation_memory_backfill_failed",
                        conversation_id=str(conversation_id),
                    )

        history = trim_history(
            history,
            max_tokens=responder.settings.agent_history_max_tokens,
            max_messages=responder.settings.agent_history_messages,
        )

        try:
            await memory.append(
                str(conversation_id),
                ConversationTurn(role="user", content=prompt),
            )
        except Exception:
            logger.warning(
                "conversation_memory_write_failed",
                conversation_id=str(conversation_id),
                role="user",
            )

        outcome = await responder.run(
            AgentRequest(
                run_id=run_uuid,
                conversation_id=conversation_id,
                customer_id=customer_id,
                prompt=prompt,
                history=history,
            ),
            publish_delta,
        )

        content = outcome.final_output
        async with database.session_factory() as session:
            message = Message(
                conversation_id=conversation_id,
                role="assistant",
                content=content,
                status="completed",
            )
            session.add(message)
            await session.flush()
            conversation = await session.get(Conversation, conversation_id)
            if conversation:
                conversation.status = "active"
            agent_run = await session.get(AgentRun, run_uuid)
            if agent_run:
                agent_run.output_message_id = message.id
                agent_run.status = "completed"
                agent_run.tool_call_count = outcome.tool_call_count
                agent_run.trace = outcome.trace
                agent_run.completed_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(message)

        try:
            await memory.append(
                str(conversation_id),
                ConversationTurn(role="assistant", content=content),
            )
        except Exception:
            logger.warning(
                "conversation_memory_write_failed",
                conversation_id=str(conversation_id),
                role="assistant",
            )

        await broker.publish(
            str(conversation_id),
            "assistant.completed",
            {"run_id": run_id, "message_id": str(message.id), "content": content},
        )
    except Exception as exc:
        logger.exception(
            "assistant_reply_failed", conversation_id=str(conversation_id), run_id=run_id
        )
        async with database.session_factory() as session:
            agent_run = await session.get(AgentRun, run_uuid)
            conversation = await session.get(Conversation, conversation_id)
            if agent_run:
                agent_run.status = "failed"
                agent_run.error_type = type(exc).__name__
                agent_run.tool_call_count = getattr(exc, "tool_call_count", 0)
                agent_run.trace = getattr(exc, "safe_trace", [])
                agent_run.completed_at = datetime.now(UTC)
            if conversation:
                conversation.status = "active"
            await session.commit()
        await broker.publish(
            str(conversation_id),
            "assistant.failed",
            {"run_id": run_id, "error": type(exc).__name__},
        )


async def list_messages(database: Database, conversation_id: uuid.UUID) -> list[Message]:
    async with database.session_factory() as session:
        result = await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.id)
        )
        return list(result)


async def list_recent_messages(
    database: Database,
    conversation_id: uuid.UUID,
    *,
    exclude_message_id: uuid.UUID,
    limit: int,
) -> list[Message]:
    async with database.session_factory() as session:
        result = await session.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.id != exclude_message_id,
                Message.role.in_(["user", "assistant"]),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        return list(reversed(list(result)))
