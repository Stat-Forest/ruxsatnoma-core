"""Every SQL statement `public` issues. No business rules here — the service
decides, the repo asks."""

import uuid
from typing import Any

from sqlalchemy import Row, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base
from app.modules.admin.models import ActivityType, Organization
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant
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


async def get_application_status_row(db: AsyncSession, *, number: str) -> Row[Any] | None:
    """One row for `GET /public/applications/check` (task 4): the application's
    own `number`/`status`/`submitted_at`, its applicant's `phone` (the contact
    the service matches against) and the display names of its activity type
    and assigned organization (the leshoz) — nothing else. No contour, no
    calculation, no document, no reviewing official; the applicant's own
    NAME is deliberately not selected either, unlike `phone`, which is read
    only to be compared and never echoed back.

    Filtered by `number` alone, mirroring `get_by_number`'s appeals
    counterpart — the phone match happens in the service, so a wrong number
    and a wrong phone answer identically (`check_application_status`'s own
    docstring)."""
    stmt = (
        select(
            Application.number,
            Application.status,
            Application.submitted_at,
            Applicant.phone,
            ActivityType.name.label("activity_type_name"),
            Organization.name.label("organization_name"),
        )
        .join(Applicant, Applicant.id == Application.applicant_id)
        .outerjoin(ActivityType, ActivityType.id == Application.activity_type_id)
        .outerjoin(Organization, Organization.id == Application.assigned_org_id)
        .where(Application.number == number)
    )
    return (await db.execute(stmt)).first()


async def rating_histogram(db: AsyncSession) -> dict[int, int]:
    """Count of `permit_ratings` rows per score, 1-5, across every
    organization — the national total `service.rating_summary` decides
    whether to publish. Missing scores are absent from the result, not zero;
    the service fills the 1-5 range only once it already knows the total
    clears the threshold."""
    rows = await db.execute(select(PermitRating.score, func.count()).group_by(PermitRating.score))
    return {int(score): int(count) for score, count in rows.all()}
