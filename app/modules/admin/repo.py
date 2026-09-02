"""admin repository: reference-data reads and writes. No business rules here."""

import uuid
from collections.abc import Iterable
from datetime import date

from sqlalchemy import Select, and_, delete, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import MediaFile
from app.core.time import business_today
from app.db import Base
from app.modules.admin.models import (
    ActivityType,
    Announcement,
    AnnouncementFile,
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


async def get_region(db: AsyncSession, region_id: uuid.UUID) -> Region | None:
    """One region by id — the sibling of `get_organization` below, for a caller that
    already holds the id and needs the row's own name (permits 3.11a composes the
    holder's address for `tz/13` requisite 11). `list_regions` above answers the
    different question of what may be CHOSEN."""
    return await db.get(Region, region_id)


async def get_district(db: AsyncSession, district_id: uuid.UUID) -> District | None:
    """One district by id. See `get_region` above."""
    return await db.get(District, district_id)


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


async def get_agency(db: AsyncSession) -> Organization | None:
    """The single root organization, if it has been created yet (ruling 6)."""
    stmt = select(Organization).where(Organization.kind == "agency")
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


async def get_activity_type(db: AsyncSession, activity_type_id: uuid.UUID) -> ActivityType | None:
    """One activity type by id, whatever its status — the sibling of
    `get_organization`/`get_classifier_item` above, for a module that already
    holds the id and needs the row's own name (permits 3.11a copies it into the
    immutable document snapshot). Archived rows are returned deliberately: a
    permit issued years ago must still resolve the activity it was issued for."""
    return await db.get(ActivityType, activity_type_id)


async def get_livestock_types_by_code(
    db: AsyncSession, codes: Iterable[str]
) -> dict[str, LivestockType]:
    """The named livestock types, keyed by code, whatever their status — the bulk
    sibling of `get_activity_type` above and for the same reason: permits 3.11a
    copies these names into an immutable document snapshot (`tz/13` requisites
    12-15), and a permit issued years ago must still resolve the species it was
    issued for. `list_livestock_types` below filters to `active` because it answers
    a different question — what may be CHOSEN today.

    A code with no row is simply absent from the result; the caller decides what a
    missing one means, since only it knows whether the code came from a form or from
    a frozen calculation."""
    codes = list(codes)
    if not codes:
        return {}
    stmt = select(LivestockType).where(LivestockType.code.in_(codes))
    return {row.code: row for row in (await db.execute(stmt)).scalars()}


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
    asked for — an admin editing history needs them, a form does not.

    A past `on_date` is a historical lookup (what WAS in force then): the date range
    alone decides it, status is irrelevant, because `supersede` (ruling 7) leaves the
    superseded row `archived` forever even though it was correctly live during its
    own `[valid_from, valid_to]` window.

    An omitted, current, or future `on_date` is instead a "what is usable now"
    lookup, and additionally requires `status == 'active'` — matching the sibling
    `list_activity_types` / `list_livestock_types` pattern, since the date range
    alone cannot tell "pulled early via `/archive`, date window not yet expired"
    apart from "still genuinely current." The future case folds in deliberately:
    `on_date` is a client-supplied parameter on a public endpoint, and a caller that
    always sends the selected date (defaulting to today) must not reproduce the bug
    this guards against.
    """
    stmt = select(ClassifierItem).where(ClassifierItem.classifier_id == classifier_id)
    if not include_archived:
        today = business_today()
        day = on_date or today
        stmt = stmt.where(
            ClassifierItem.valid_from <= day,
            (ClassifierItem.valid_to.is_(None)) | (ClassifierItem.valid_to >= day),
        )
        if on_date is None or on_date >= today:
            stmt = stmt.where(ClassifierItem.status == "active")
    stmt = stmt.order_by(ClassifierItem.sort_order, ClassifierItem.code)
    return list((await db.execute(stmt)).scalars())


# --- Announcements (Task 8): admin CRUD queries + the audience-visibility clause ----


async def get_announcement(db: AsyncSession, announcement_id: uuid.UUID) -> Announcement | None:
    return await db.get(Announcement, announcement_id)


async def list_admin_announcements(
    db: AsyncSession, *, status: str | None, offset: int, limit: int
) -> tuple[list[Announcement], int]:
    stmt = select(Announcement)
    if status is not None:
        stmt = stmt.where(Announcement.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(stmt.order_by(Announcement.created_at.desc()).offset(offset).limit(limit))
    ).scalars()
    return list(rows), total


def announcement_visibility_clause(role_code: str, region_id: uuid.UUID | None):
    """A row is visible when published, currently inside its publish window, and its
    audience (if any) matches the reader's role/region — a missing key or null
    `audience` column means no restriction on that axis (models.py's Announcement
    docstring, ruling 12). Shared by the reader queries below and by
    `file_visible_via_announcement` (the files-access grant, Task 8 ruling 5)."""
    aud = Announcement.audience
    now = func.now()
    # jsonb `?` on an array = element membership; SQLAlchemy spells it .has_key()
    role_ok = or_(aud.is_(None), aud["role_codes"].is_(None), aud["role_codes"].has_key(role_code))
    region_parts = [aud.is_(None), aud["region_ids"].is_(None)]
    if region_id is not None:
        region_parts.append(aud["region_ids"].has_key(str(region_id)))
    return and_(
        Announcement.status == "published",
        Announcement.publish_from <= now,
        or_(Announcement.publish_to.is_(None), Announcement.publish_to >= now),
        role_ok,
        or_(*region_parts),
    )


async def list_visible_announcements(
    db: AsyncSession, *, role_code: str, region_id: uuid.UUID | None, offset: int, limit: int
) -> tuple[list[Announcement], int]:
    clause = announcement_visibility_clause(role_code, region_id)
    stmt = select(Announcement).where(clause)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Announcement.publish_from.desc()).offset(offset).limit(limit)
        )
    ).scalars()
    return list(rows), total


async def get_visible_announcement(
    db: AsyncSession, announcement_id: uuid.UUID, *, role_code: str, region_id: uuid.UUID | None
) -> Announcement | None:
    clause = announcement_visibility_clause(role_code, region_id)
    stmt = select(Announcement).where(Announcement.id == announcement_id, clause)
    return (await db.execute(stmt)).scalar_one_or_none()


async def file_visible_via_announcement(
    db: AsyncSession, file_id: uuid.UUID, *, role_code: str, region_id: uuid.UUID | None
) -> bool:
    """Backs `announcements_service.announcement_grants_access` (the
    `files.ACCESS_CHECKS` entry, ruling 5): true when `file_id` is attached to an
    announcement currently visible to a reader with this role/region."""
    clause = announcement_visibility_clause(role_code, region_id)
    stmt = select(
        exists(
            select(1)
            .select_from(AnnouncementFile)
            .join(Announcement, Announcement.id == AnnouncementFile.announcement_id)
            .where(AnnouncementFile.file_id == file_id, clause)
        )
    )
    return bool((await db.execute(stmt)).scalar_one())


async def count_active_media_files(db: AsyncSession, file_ids: list[uuid.UUID]) -> int:
    """One-SELECT existence+status check backing `file_ids` validation on
    create/patch — an archived file can't be (re)attached."""
    if not file_ids:
        return 0
    stmt = (
        select(func.count())
        .select_from(MediaFile)
        .where(MediaFile.id.in_(file_ids), MediaFile.status == "active")
    )
    return (await db.execute(stmt)).scalar_one()


async def set_announcement_files(
    db: AsyncSession, announcement_id: uuid.UUID, file_ids: list[uuid.UUID]
) -> None:
    """Replace-set: drop the previous links, insert the given ones in order
    (`position` = list index)."""
    await db.execute(
        delete(AnnouncementFile).where(AnnouncementFile.announcement_id == announcement_id)
    )
    for position, file_id in enumerate(file_ids):
        db.add(
            AnnouncementFile(announcement_id=announcement_id, file_id=file_id, position=position)
        )
    await db.flush()


async def list_announcement_files(db: AsyncSession, announcement_id: uuid.UUID) -> list[MediaFile]:
    stmt = (
        select(MediaFile)
        .join(AnnouncementFile, AnnouncementFile.file_id == MediaFile.id)
        .where(AnnouncementFile.announcement_id == announcement_id)
        .order_by(AnnouncementFile.position)
    )
    return list((await db.execute(stmt)).scalars())
