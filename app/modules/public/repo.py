"""Every SQL statement `public` issues. No business rules here — the service
decides, the repo asks."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base
from app.modules.permits.models import PermitRating
from app.modules.public.models import CitizenAppeal


async def add(db: AsyncSession, obj: Base) -> None:
    db.add(obj)
    await db.flush()


async def get_by_id(db: AsyncSession, appeal_id: uuid.UUID) -> CitizenAppeal | None:
    return await db.get(CitizenAppeal, appeal_id)


async def get_by_number(db: AsyncSession, number: str) -> CitizenAppeal | None:
    return await db.scalar(select(CitizenAppeal).where(CitizenAppeal.number == number))


async def list_appeals(
    db: AsyncSession, *, status: str | None, offset: int, limit: int
) -> tuple[list[CitizenAppeal], int]:
    stmt = select(CitizenAppeal)
    if status is not None:
        stmt = stmt.where(CitizenAppeal.status == status)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await db.execute(stmt.order_by(CitizenAppeal.created_at.desc()).offset(offset).limit(limit))
    ).scalars()
    return list(rows), total or 0


async def rating_histogram(db: AsyncSession) -> dict[int, int]:
    """Count of `permit_ratings` rows per score, 1-5, across every
    organization — the national total `service.rating_summary` decides
    whether to publish. Missing scores are absent from the result, not zero;
    the service fills the 1-5 range only once it already knows the total
    clears the threshold."""
    rows = await db.execute(select(PermitRating.score, func.count()).group_by(PermitRating.score))
    return {int(score): int(count) for score, count in rows.all()}
