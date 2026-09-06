"""Every SQL statement `public` issues. No business rules here — the service
decides, the repo asks."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base
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
