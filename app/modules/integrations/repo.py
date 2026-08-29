"""DB queries of the integrations module."""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.integrations.models import InboundDeadLetter, OutboxMessage


async def pick_due(
    db: AsyncSession, *, exclude_destinations: Sequence[str] = ()
) -> OutboxMessage | None:
    """Claim one due row; the row lock is held until the caller commits/rolls back.

    SKIP LOCKED lets any number of worker processes drain the queue without
    stepping on each other (plan 03.4 ruling 5).

    Uses clock_timestamp(), not now(): Postgres freezes now() for a
    transaction's whole lifetime, so a caller that reuses one session across
    several calls (the outbox worker opens a fresh session per call and never
    notices, but a drain loop sharing one session — e.g. tests — does) would
    otherwise keep comparing against a timestamp stuck at whenever this
    session's transaction first began, never seeing rows enqueued afterwards.

    A destination whose breaker is open is excluded here rather than after the
    claim — an outage must not consume the retry budget of every queued row.
    """
    stmt = select(OutboxMessage).where(
        OutboxMessage.status == "pending",
        OutboxMessage.next_attempt_at <= func.clock_timestamp(),
    )
    if exclude_destinations:
        stmt = stmt.where(OutboxMessage.destination.notin_(list(exclude_destinations)))
    return (
        await db.execute(
            stmt.order_by(OutboxMessage.next_attempt_at).limit(1).with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()


async def get_message(db: AsyncSession, message_id: uuid.UUID) -> OutboxMessage | None:
    return await db.get(OutboxMessage, message_id)


async def get_dead_letter(db: AsyncSession, letter_id: uuid.UUID) -> InboundDeadLetter | None:
    return await db.get(InboundDeadLetter, letter_id)


async def list_outbox(
    db: AsyncSession,
    *,
    status: str | None,
    destination: str | None,
    page: int,
    page_size: int,
) -> tuple[list[OutboxMessage], int]:
    stmt = select(OutboxMessage)
    if status is not None:
        stmt = stmt.where(OutboxMessage.status == status)
    if destination is not None:
        stmt = stmt.where(OutboxMessage.destination == destination)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(OutboxMessage.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars()
    return list(rows), total


async def list_dead_letters(
    db: AsyncSession, *, status: str | None, page: int, page_size: int
) -> tuple[list[InboundDeadLetter], int]:
    stmt = select(InboundDeadLetter)
    if status is not None:
        stmt = stmt.where(InboundDeadLetter.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(InboundDeadLetter.received_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars()
    return list(rows), total
