from __future__ import annotations

import uuid

from sqlalchemy import select

from backend.app.agents.contracts import CreateHumanTicketArgs, ToolResult
from backend.app.database import Database
from backend.app.models import HumanTicket


class TicketService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(
        self,
        *,
        run_id: uuid.UUID,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        request: CreateHumanTicketArgs,
    ) -> ToolResult:
        async with self.database.session_factory() as session:
            ticket = await session.scalar(
                select(HumanTicket).where(HumanTicket.agent_run_id == run_id)
            )
            if ticket is None:
                ticket = HumanTicket(
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    agent_run_id=run_id,
                    reason=request.reason,
                    summary=request.summary,
                    priority=request.priority,
                )
                session.add(ticket)
                await session.commit()
                await session.refresh(ticket)
        return ToolResult(
            success=True,
            error_code=None,
            message="人工工单已创建",
            data={"ticket_id": str(ticket.id), "status": ticket.status},
        )
