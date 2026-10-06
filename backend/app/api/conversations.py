from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from backend.app.agents.guardrails import UnsafeInputError, validate_user_input
from backend.app.messaging import AGENT_REPLY_REQUESTED
from backend.app.models import AgentRun, Conversation, Message, OutboxEvent
from backend.app.schemas import ConversationRead, MessageAccepted, MessageCreate, MessageRead
from backend.app.services.conversations import list_messages, process_assistant_reply
from backend.app.security.sessions import require_customer
from backend.app.token_budget import estimate_text_tokens


router = APIRouter(prefix="/conversations", tags=["conversations"])
logger = structlog.get_logger()


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_message(
    conversation_id: uuid.UUID,
    payload: MessageCreate,
    request: Request,
    customer_id: uuid.UUID = Depends(require_customer),
) -> MessageAccepted:
    try:
        content = validate_user_input(payload.content)
    except UnsafeInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    current_tokens = estimate_text_tokens(content)
    current_token_limit = request.app.state.settings.agent_current_message_max_tokens
    if current_tokens > current_token_limit:
        raise HTTPException(
            status_code=422,
            detail=(
                f"消息过长，估算为 {current_tokens} tokens，"
                f"最多允许 {current_token_limit} tokens"
            ),
        )
    database = request.app.state.database
    message_id = uuid.uuid4()
    run_id = uuid.uuid4()
    outbox_event_id = uuid.uuid4()
    async with database.session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            conversation = Conversation(
                id=conversation_id,
                customer_id=customer_id,
                status="processing",
                channel="web",
            )
            session.add(conversation)
        else:
            if conversation.customer_id is not None and conversation.customer_id != customer_id:
                raise HTTPException(status_code=403, detail="Conversation access denied")
            if conversation.customer_id is None:
                conversation.customer_id = customer_id
            conversation.status = "processing"
        message = Message(
            id=message_id,
            conversation_id=conversation_id,
            role="user",
            content=content,
            status="completed",
        )
        session.add(message)
        # AgentRun has an input_message_id foreign key but no ORM relationship
        # to Message. Flush the conversation and input message first so
        # PostgreSQL observes the dependency while keeping one transaction.
        await session.flush()
        session.add(
            AgentRun(
                id=run_id,
                conversation_id=conversation_id,
                input_message_id=message_id,
                status="queued",
                model=(
                    request.app.state.settings.kimi_model
                    if request.app.state.settings.agent_mode == "live"
                    else "mock"
                ),
                trace=[],
            )
        )
        session.add(
            OutboxEvent(
                id=outbox_event_id,
                aggregate_type="agent_run",
                aggregate_id=run_id,
                event_type=AGENT_REPLY_REQUESTED,
                payload={
                    "run_id": str(run_id),
                    "conversation_id": str(conversation_id),
                    "input_message_id": str(message_id),
                },
                idempotency_key=f"agent-reply:{run_id}",
                status="PENDING",
            )
        )
        await session.commit()
        await session.refresh(message)

    try:
        await request.app.state.broker.publish(
            str(conversation_id),
            "user_message.created",
            {"message_id": str(message.id), "role": "user", "content": message.content},
        )
    except Exception:
        # PostgreSQL already owns the durable message and command. Returning an
        # HTTP error here could make a client retry and create a duplicate.
        logger.exception(
            "user_message_event_publish_failed",
            conversation_id=str(conversation_id),
            message_id=str(message.id),
        )

    if request.app.state.settings.agent_task_backend == "inline":
        await process_assistant_reply(
            database=database,
            broker=request.app.state.broker,
            memory=request.app.state.conversation_memory,
            responder=request.app.state.responder,
            run_id=run_id,
            final_attempt=True,
        )
        async with database.session_factory() as session:
            event = await session.get(OutboxEvent, outbox_event_id, with_for_update=True)
            if event is not None and event.status == "PENDING":
                event.status = "PUBLISHED"
                event.processed_at = datetime.now(UTC)
                await session.commit()
    return MessageAccepted(
        conversation_id=conversation_id, message=MessageRead.model_validate(message)
    )


@router.get("/{conversation_id}", response_model=ConversationRead)
async def get_conversation(
    conversation_id: uuid.UUID,
    request: Request,
    customer_id: uuid.UUID = Depends(require_customer),
) -> ConversationRead:
    database = request.app.state.database
    async with database.session_factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.id == conversation_id)
        )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation.customer_id is not None and conversation.customer_id != customer_id:
        raise HTTPException(status_code=403, detail="Conversation access denied")
    messages = await list_messages(database, conversation_id)
    return ConversationRead(
        id=conversation.id,
        status=conversation.status,
        channel=conversation.channel,
        messages=[MessageRead.model_validate(message) for message in messages],
    )


@router.get("/{conversation_id}/events")
async def stream_events(
    conversation_id: uuid.UUID,
    request: Request,
    customer_id: uuid.UUID = Depends(require_customer),
    cursor: str | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    database = request.app.state.database
    async with database.session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if (
        conversation.customer_id is not None
        and conversation.customer_id != customer_id
    ):
        raise HTTPException(status_code=403, detail="Conversation access denied")
    broker = request.app.state.broker
    initial_cursor = cursor or last_event_id or "0-0"

    async def generate():
        current = initial_cursor
        # Flush response headers immediately so EventSource.onopen does not
        # wait for the first message or 15-second heartbeat.
        yield ServerSentEvent(comment="connected")
        while not await request.is_disconnected():
            events = await broker.read(str(conversation_id), current)
            for item in events:
                current = item.id
                yield ServerSentEvent(
                    id=item.id,
                    event=item.event,
                    data=json.dumps(item.data, ensure_ascii=False),
                )

    return EventSourceResponse(
        generate(), ping=15, headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
