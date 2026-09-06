"""Every SQL statement `help` issues. No business rules here — the service
decides, the repo asks."""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base
from app.modules.help.models import FaqItem, SupportTicket, SupportTicketMessage


async def add(db: AsyncSession, obj: Base) -> None:
    db.add(obj)
    await db.flush()


async def list_faq(db: AsyncSession, *, status: str | None) -> list[FaqItem]:
    stmt = select(FaqItem)
    if status is not None:
        stmt = stmt.where(FaqItem.status == status)
    rows = await db.execute(stmt.order_by(FaqItem.sort_order, FaqItem.created_at))
    return list(rows.scalars())


async def get_faq(db: AsyncSession, faq_id: uuid.UUID) -> FaqItem | None:
    return await db.get(FaqItem, faq_id)


async def get_ticket(db: AsyncSession, ticket_id: uuid.UUID) -> SupportTicket | None:
    return await db.get(SupportTicket, ticket_id)


async def list_tickets(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    status: str | None,
    offset: int,
    limit: int,
) -> tuple[list[SupportTicket], int]:
    """`user_id=None` means every ticket — the caller (service) decides that
    based on `help.tickets.manage`, never this function."""
    stmt = select(SupportTicket)
    if user_id is not None:
        stmt = stmt.where(
            or_(SupportTicket.user_id == user_id, SupportTicket.assigned_to == user_id)
        )
    if status is not None:
        stmt = stmt.where(SupportTicket.status == status)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = await db.execute(
        stmt.order_by(SupportTicket.created_at.desc()).offset(offset).limit(limit)
    )
    return list(rows.scalars()), total or 0


async def list_messages(db: AsyncSession, ticket_id: uuid.UUID) -> list[SupportTicketMessage]:
    rows = await db.execute(
        select(SupportTicketMessage)
        .where(SupportTicketMessage.ticket_id == ticket_id)
        .order_by(SupportTicketMessage.created_at)
    )
    return list(rows.scalars())
