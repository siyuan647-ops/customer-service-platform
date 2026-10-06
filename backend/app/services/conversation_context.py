from __future__ import annotations

import uuid

from pydantic import ValidationError
from sqlalchemy import select

from backend.app.agents.contracts import ConversationContext
from backend.app.database import Database
from backend.app.models import Conversation


class ConversationContextService:
    """Persists validated business references independently of LLM history."""

    def __init__(self, database: Database | None) -> None:
        self.database = database

    async def get(
        self,
        *,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
    ) -> ConversationContext:
        if self.database is None:
            return ConversationContext()
        async with self.database.session_factory() as session:
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.customer_id == customer_id,
                )
            )
        if conversation is None:
            return ConversationContext()
        try:
            return ConversationContext.model_validate(conversation.context_data or {})
        except ValidationError:
            # A malformed optional context must never make the conversation unusable.
            return ConversationContext()

    async def set_active_order(
        self,
        *,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        order_id: str,
        order_item_id: str | None = None,
    ) -> None:
        if self.database is None:
            return
        normalized_order_id = order_id.strip().upper()
        normalized_item_id = order_item_id.strip().upper() if order_item_id else None
        # Validate before writing. Ownership must already have been verified by
        # OrderService; this service additionally scopes the conversation row.
        ConversationContext(
            active_order_id=normalized_order_id,
            active_order_item_id=normalized_item_id,
        )
        async with self.database.session_factory() as session:
            conversation = await session.scalar(
                select(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.customer_id == customer_id,
                )
                .with_for_update()
            )
            if conversation is None:
                return
            data = dict(conversation.context_data or {})
            previous_order_id = data.get("active_order_id")
            data["active_order_id"] = normalized_order_id
            if previous_order_id != normalized_order_id or order_item_id is not None:
                data["active_order_item_id"] = normalized_item_id
            conversation.context_data = data
            await session.commit()

    async def clear_active_order(
        self,
        *,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
    ) -> None:
        if self.database is None:
            return
        async with self.database.session_factory() as session:
            conversation = await session.scalar(
                select(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.customer_id == customer_id,
                )
                .with_for_update()
            )
            if conversation is None:
                return
            data = dict(conversation.context_data or {})
            data["active_order_id"] = None
            data["active_order_item_id"] = None
            conversation.context_data = data
            await session.commit()
