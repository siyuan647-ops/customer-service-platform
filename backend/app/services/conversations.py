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
from backend.app.security.circuit_breaker import CircuitOpenError


logger = structlog.get_logger()


async def process_assistant_reply(
    *,
    database: Database,
    broker: EventBroker,
    memory: ConversationMemory,
    responder: SupervisorAgent,
    run_id: uuid.UUID,
    final_attempt: bool,
) -> str:
    run_uuid = run_id
    run_id_text = str(run_uuid)
    async with database.session_factory() as session:
        agent_run = await session.get(AgentRun, run_uuid, with_for_update=True)
        if agent_run is None:
            raise RuntimeError("AgentRun does not exist")
        if agent_run.status == "completed" and agent_run.output_message_id is not None:
            return "already_completed"
        if agent_run.status == "failed":
            return "already_failed"
        conversation = await session.get(Conversation, agent_run.conversation_id)
        input_message = await session.get(Message, agent_run.input_message_id)
        if conversation is None or input_message is None or conversation.customer_id is None:
            raise RuntimeError("AgentRun references incomplete conversation data")
        conversation_id = conversation.id
        customer_id = conversation.customer_id
        input_message_id = input_message.id
        prompt = input_message.content
        agent_run.status = "running"
        agent_run.error_type = None
        await session.commit()

    await _safe_publish(
        broker,
        str(conversation_id),
        "assistant.started",
        {"run_id": run_id_text},
    )
    try:
        async def publish_delta(chunk: str) -> None:
            await _safe_publish(
                broker,
                str(conversation_id),
                "assistant.delta",
                {"run_id": run_id_text, "delta": chunk},
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

        await _safe_publish(
            broker,
            str(conversation_id),
            "assistant.completed",
            {"run_id": run_id_text, "message_id": str(message.id), "content": content},
        )
        return "completed"
    except Exception as exc:
        logger.exception(
            "assistant_reply_failed",
            conversation_id=str(conversation_id),
            run_id=run_id_text,
            final_attempt=final_attempt,
        )
        async with database.session_factory() as session:
            agent_run = await session.get(AgentRun, run_uuid, with_for_update=True)
            conversation = await session.get(Conversation, conversation_id)
            if agent_run:
                agent_run.status = (
                    "retrying" if isinstance(exc, CircuitOpenError) or not final_attempt else "failed"
                )
                agent_run.error_type = type(exc).__name__
                agent_run.tool_call_count = getattr(exc, "tool_call_count", 0)
                agent_run.trace = getattr(exc, "safe_trace", [])
                agent_run.completed_at = (
                    datetime.now(UTC) if final_attempt and not isinstance(exc, CircuitOpenError) else None
                )
            if conversation:
                conversation.status = (
                    "processing" if isinstance(exc, CircuitOpenError) or not final_attempt else "active"
                )
            await session.commit()
        if final_attempt and not isinstance(exc, CircuitOpenError):
            await _safe_publish(
                broker,
                str(conversation_id),
                "assistant.failed",
                {"run_id": run_id_text, "error": type(exc).__name__},
            )
            return "failed"
        raise


async def _safe_publish(
    broker: EventBroker,
    conversation_id: str,
    event: str,
    data: dict,
) -> None:
    try:
        await broker.publish(conversation_id, event, data)
    except Exception:
        logger.exception(
            "conversation_event_publish_failed",
            conversation_id=conversation_id,
            event=event,
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
