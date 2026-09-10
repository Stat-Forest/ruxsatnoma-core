"""Every SQL statement `public` issues against its OWN table, `citizen_appeals`.
No business rules here — the service decides, the repo asks.

Stage 8 fix wave, finding 2: this file used to also query `applications`,
`auth.models.Applicant`, `admin.models.{ActivityType,Organization}` and
`permits.models.PermitRating` directly — none of those tables are on
`backend/CLAUDE.md`'s cross-module read whitelist (reports/dashboard/search/
oversight/archive), and `applications` is named there explicitly: "never an
import of `applications.repo`/`.models`". Both reads now go through the
owning module's service — `applications.service.public_status_lookup` and
`permits.service.public_rating_histogram` — the same pattern
`public.service.open_data_stats` already used for `permits_service.
public_active_stats_by_organization`."""

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
