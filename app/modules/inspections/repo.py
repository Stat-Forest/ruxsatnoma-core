"""Every query of the inspections module."""

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.schemas import PageParams
from app.modules.admin.models import Organization
from app.modules.inspections.models import (
    Checklist,
    InspectionAct,
    InspectionActFile,
    InspectionTask,
    ViolationAppeal,
    ViolationCase,
    ViolationCaseHistory,
)


async def get_checklist(db: AsyncSession, checklist_id: uuid.UUID) -> Checklist | None:
    return await db.get(Checklist, checklist_id)


async def get_active_checklist_by_code(db: AsyncSession, code: str) -> Checklist | None:
    result = await db.execute(
        select(Checklist).where(Checklist.code == code, Checklist.status == "active")
    )
    return result.scalar_one_or_none()


async def list_active_checklists(db: AsyncSession) -> Sequence[Checklist]:
    result = await db.execute(
        select(Checklist).where(Checklist.status == "active").order_by(Checklist.code)
    )
    return result.scalars().all()


async def next_checklist_version(db: AsyncSession, code: str) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(Checklist.version), 0) + 1).where(Checklist.code == code)
    )
    return result.scalar_one()


async def get_task(db: AsyncSession, task_id: uuid.UUID) -> InspectionTask | None:
    return await db.get(InspectionTask, task_id)


async def list_tasks(
    db: AsyncSession,
    *,
    scope: ColumnElement[bool],
    status: str | None,
    params: PageParams,
) -> tuple[Sequence[InspectionTask], int]:
    # Outer join: `organization_id` is nullable (an org-less patrol) and the
    # scope's own `zone_filter` may reference `Organization.region_id`/
    # `.district_id` for a region- or district-scoped viewer — the same join
    # `search`/`reports`/`dashboard` add for exactly this reason.
    stmt = (
        select(InspectionTask)
        .outerjoin(Organization, Organization.id == InspectionTask.organization_id)
        .where(scope)
    )
    if status is not None:
        stmt = stmt.where(InspectionTask.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = (
        stmt.order_by(InspectionTask.due_at, InspectionTask.id)
        .limit(params.page_size)
        .offset(params.offset)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return rows, total


async def get_act(db: AsyncSession, act_id: uuid.UUID) -> InspectionAct | None:
    return await db.get(InspectionAct, act_id)


async def act_gps_lonlat(db: AsyncSession, act_id: uuid.UUID) -> tuple[float | None, float | None]:
    """The act's own GPS fix as plain `(lon, lat)` floats, read back through
    PostGIS (`ST_X`/`ST_Y`) rather than decoded in Python — the same
    'geometry never travels through Python' convention `gis.repo`'s own
    `version_wkt`-style helpers follow, applied to the one point geometry
    this module owns."""
    row = (
        await db.execute(
            text("SELECT ST_X(gps), ST_Y(gps) FROM inspection_acts WHERE id = :id"),
            {"id": act_id},
        )
    ).one_or_none()
    if row is None:
        return None, None
    return row[0], row[1]


async def list_acts(
    db: AsyncSession,
    *,
    scope: ColumnElement[bool],
    result: str | None,
    params: PageParams,
) -> tuple[Sequence[InspectionAct], int]:
    # Outer join for the same reason as `list_tasks` above.
    stmt = (
        select(InspectionAct)
        .outerjoin(Organization, Organization.id == InspectionAct.organization_id)
        .where(scope)
    )
    if result is not None:
        stmt = stmt.where(InspectionAct.result == result)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = (
        stmt.order_by(InspectionAct.occurred_at.desc(), InspectionAct.id)
        .limit(params.page_size)
        .offset(params.offset)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return rows, total


async def list_act_files(db: AsyncSession, act_id: uuid.UUID) -> Sequence[InspectionActFile]:
    result = await db.execute(select(InspectionActFile).where(InspectionActFile.act_id == act_id))
    return result.scalars().all()


async def get_act_file(
    db: AsyncSession, act_id: uuid.UUID, file_id: uuid.UUID
) -> InspectionActFile | None:
    result = await db.execute(
        select(InspectionActFile).where(
            InspectionActFile.act_id == act_id, InspectionActFile.file_id == file_id
        )
    )
    return result.scalar_one_or_none()


async def get_case(db: AsyncSession, case_id: uuid.UUID) -> ViolationCase | None:
    return await db.get(ViolationCase, case_id)


async def case_for_act(db: AsyncSession, act_id: uuid.UUID) -> ViolationCase | None:
    result = await db.execute(select(ViolationCase).where(ViolationCase.act_id == act_id))
    return result.scalar_one_or_none()


async def list_cases(
    db: AsyncSession,
    *,
    scope: ColumnElement[bool],
    status: str | None,
    params: PageParams,
) -> tuple[Sequence[ViolationCase], int]:
    # Outer join for the same reason as `list_tasks` above.
    stmt = (
        select(ViolationCase)
        .outerjoin(Organization, Organization.id == ViolationCase.organization_id)
        .where(scope)
    )
    if status is not None:
        stmt = stmt.where(ViolationCase.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = (
        stmt.order_by(ViolationCase.created_at.desc(), ViolationCase.id)
        .limit(params.page_size)
        .offset(params.offset)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return rows, total


async def list_case_history(db: AsyncSession, case_id: uuid.UUID) -> Sequence[ViolationCaseHistory]:
    result = await db.execute(
        select(ViolationCaseHistory)
        .where(ViolationCaseHistory.case_id == case_id)
        .order_by(ViolationCaseHistory.occurred_at, ViolationCaseHistory.id)
    )
    return result.scalars().all()


async def list_appeals(db: AsyncSession, case_id: uuid.UUID) -> Sequence[ViolationAppeal]:
    result = await db.execute(
        select(ViolationAppeal)
        .where(ViolationAppeal.case_id == case_id)
        .order_by(ViolationAppeal.filed_at)
    )
    return result.scalars().all()


async def get_open_appeal(db: AsyncSession, case_id: uuid.UUID) -> ViolationAppeal | None:
    """The most recent appeal on this case with no resolution yet, or `None`.
    `or_` (not chained `.where` calls) so both conditions apply to the SAME
    row inside one predicate, matching how every other module in this
    codebase reads an "open X" row."""
    result = await db.execute(
        select(ViolationAppeal)
        .where(ViolationAppeal.case_id == case_id, ViolationAppeal.resolved_at.is_(None))
        .order_by(ViolationAppeal.filed_at.desc())
    )
    return result.scalars().first()
