"""admin repository: reference-data reads and writes. No business rules here."""

import uuid
from datetime import date

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base
from app.modules.admin.models import (
    ActivityType,
    Classifier,
    ClassifierItem,
    District,
    LivestockType,
    Organization,
    Region,
)


async def add(db: AsyncSession, obj: Base) -> None:
    db.add(obj)
    await db.flush()


async def list_regions(db: AsyncSession) -> list[Region]:
    stmt = select(Region).order_by(Region.sort_order, Region.code)
    return list((await db.execute(stmt)).scalars())


async def list_districts(db: AsyncSession, region_id: uuid.UUID | None) -> list[District]:
    stmt = select(District).order_by(District.sort_order, District.code)
    if region_id is not None:
        stmt = stmt.where(District.region_id == region_id)
    return list((await db.execute(stmt)).scalars())


def _organizations_query(
    *,
    parent_id: uuid.UUID | None,
    kind: str | None,
    region_id: uuid.UUID | None,
    status: str | None,
) -> Select[tuple[Organization]]:
    """No filter at all → the root only (ruling 11): the tree is walked level by level."""
    stmt = select(Organization)
    if parent_id is not None:
        stmt = stmt.where(Organization.parent_id == parent_id)
    elif kind is None and region_id is None:
        stmt = stmt.where(Organization.kind == "agency")
    if kind is not None:
        stmt = stmt.where(Organization.kind == kind)
    if region_id is not None:
        stmt = stmt.where(Organization.region_id == region_id)
    if status is not None:
        stmt = stmt.where(Organization.status == status)
    return stmt


async def list_organizations(
    db: AsyncSession,
    *,
    parent_id: uuid.UUID | None = None,
    kind: str | None = None,
    region_id: uuid.UUID | None = None,
    status: str | None = "active",
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[Organization], int]:
    stmt = _organizations_query(parent_id=parent_id, kind=kind, region_id=region_id, status=status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(stmt.order_by(Organization.code).offset(offset).limit(limit))
    ).scalars()
    return list(rows), total


async def get_organization(db: AsyncSession, org_id: uuid.UUID) -> Organization | None:
    return await db.get(Organization, org_id)


async def get_organization_by_code(db: AsyncSession, code: str) -> Organization | None:
    stmt = select(Organization).where(Organization.code == code)
    return (await db.execute(stmt)).scalar_one_or_none()


async def is_descendant(
    db: AsyncSession, *, ancestor_id: uuid.UUID, candidate_id: uuid.UUID
) -> bool:
    """True when `candidate_id` is inside `ancestor_id`'s subtree — the guard against a
    re-parent that would create a cycle (ruling 6). Plain SQL: a self-referential
    recursive CTE is far more readable this way than through the ORM CTE API."""
    stmt = text(
        """
        WITH RECURSIVE subtree AS (
            SELECT id FROM organizations WHERE id = :ancestor_id
            UNION ALL
            SELECT o.id FROM organizations o JOIN subtree s ON o.parent_id = s.id
        )
        SELECT EXISTS (SELECT 1 FROM subtree WHERE id = :candidate_id)
        """
    ).bindparams(ancestor_id=ancestor_id, candidate_id=candidate_id)
    return bool((await db.execute(stmt)).scalar_one())


async def list_activity_types(db: AsyncSession) -> list[ActivityType]:
    stmt = (
        select(ActivityType)
        .where(ActivityType.status == "active")
        .order_by(ActivityType.sort_order, ActivityType.code)
    )
    return list((await db.execute(stmt)).scalars())


async def list_livestock_types(db: AsyncSession) -> list[LivestockType]:
    stmt = (
        select(LivestockType)
        .where(LivestockType.status == "active")
        .order_by(LivestockType.sort_order, LivestockType.code)
    )
    return list((await db.execute(stmt)).scalars())


async def get_classifier_by_code(db: AsyncSession, code: str) -> Classifier | None:
    stmt = select(Classifier).where(Classifier.code == code)
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_classifier_item(db: AsyncSession, item_id: uuid.UUID) -> ClassifierItem | None:
    return await db.get(ClassifierItem, item_id)


async def list_classifier_items(
    db: AsyncSession,
    classifier_id: uuid.UUID,
    *,
    on_date: date | None = None,
    include_archived: bool = False,
) -> list[ClassifierItem]:
    """Items valid on `on_date` (default: today). Archived rows are included only when
    asked for — an admin editing history needs them, a form does not."""
    stmt = select(ClassifierItem).where(ClassifierItem.classifier_id == classifier_id)
    if not include_archived:
        day = on_date or date.today()
        stmt = stmt.where(
            ClassifierItem.valid_from <= day,
            (ClassifierItem.valid_to.is_(None)) | (ClassifierItem.valid_to >= day),
        )
    stmt = stmt.order_by(ClassifierItem.sort_order, ClassifierItem.code)
    return list((await db.execute(stmt)).scalars())
