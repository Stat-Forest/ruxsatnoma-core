"""Cross-module reads (design/01 rule 5: a reader gets direct, read-only
access to any table). Every function here ANDs its own filters onto a
mandatory `scope` predicate the SERVICE built from `abac.zone_filter` — this
file never decides who may see what, only how to query once that is decided
(same split `permits.repo.list_permits` uses).

No persisted full-text index (plan ruling 1): `_text_filter` below is a live
`ILIKE` (case-insensitive substring, `unaccent`-normalized on both sides) with
a `similarity()` tiebreak for ordering — `pg_trgm`/`unaccent` are both
extensions already installed by migration `0001`, so this costs no schema
change to any table, including this module's own.

This file, plus `saved_filters` CRUD below, is the entire read surface:
`applications`/`permits`/`applicants` are read here directly and never
written."""

import uuid
from typing import Any

from sqlalchemy import Row, String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant
from app.modules.permits.models import Permit
from app.modules.search.models import SavedFilter


def _text_filter(pattern: str, *columns: Any) -> Any:
    """`ILIKE '%needle%'` OR'd across every given column, `unaccent`-wrapped
    on both sides so a diacritic in either the query or the stored value does
    not hide a match — the same compensation `design/02` names for the
    tsvector approach this module does not take (plan ruling 1). A literal
    `%`/`_`/`\\` typed by the caller is escaped first so it matches itself
    rather than acting as an ILIKE wildcard."""
    escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    needle = func.unaccent(f"%{escaped}%")
    return or_(*(func.unaccent(col).ilike(needle) for col in columns))


def _similarity_rank(q: str, *columns: Any) -> Any:
    """The BEST trigram similarity across the given columns, highest first —
    a ranking hint only (`_text_filter` above is what actually admits or
    rejects a row), so a typo'd query still surfaces its closest matches on
    top of an otherwise ILIKE-ordered page."""
    scores = [func.similarity(col, q) for col in columns]
    best = scores[0]
    for s in scores[1:]:
        best = func.greatest(best, s)
    return best


async def search_applications(
    db: AsyncSession,
    *,
    scope: Any,
    q: str | None,
    status: str | None,
    organization_id: uuid.UUID | None,
    activity_type_id: uuid.UUID | None,
    offset: int,
    limit: int,
) -> tuple[list[Row], int]:
    """One page of applications matching `scope` and the given filters, with
    the total. `scope` is `abac.zone_filter`'s expression built by the
    service over `Organization.region_id/district_id` + `Application.
    assigned_org_id` — required, exactly as `permits.repo.list_permits`
    documents for its own `scope` argument: a read of this table with no
    scope at all is every application in the country.

    LEFT JOINs `organizations` (unlike `permits.repo.list_permits`'s INNER
    JOIN): `assigned_org_id` is nullable, and an unassigned row must still be
    visible to a republic-wide actor (`zone_filter` returns `true()` for one,
    which does not depend on the join at all) while correctly disappearing
    for a zone-scoped one (whose `zone_filter` condition compares against a
    NULL `Organization` column through the LEFT JOIN and evaluates false)."""
    conditions: list[Any] = [scope]
    for column, value in (
        (Application.status, status),
        (Application.assigned_org_id, organization_id),
        (Application.activity_type_id, activity_type_id),
    ):
        if value is not None:
            conditions.append(column == value)
    if q:
        conditions.append(_text_filter(q, Application.number, Applicant.name, Applicant.phone))

    base = (
        select(
            Application.id,
            Application.number,
            Application.status,
            Application.assigned_org_id,
            Applicant.name.label("applicant_name"),
            Application.created_at,
        )
        .join(Applicant, Applicant.id == Application.applicant_id)
        .outerjoin(Organization, Organization.id == Application.assigned_org_id)
        .where(*conditions)
    )
    total = (
        await db.execute(
            select(func.count()).select_from(base.with_only_columns(Application.id).subquery())
        )
    ).scalar_one()
    order = Application.id.desc()
    if q:
        order = _similarity_rank(q, Application.number, Applicant.name).desc()
    rows = await db.execute(base.order_by(order, Application.id.desc()).offset(offset).limit(limit))
    return list(rows.all()), total


async def search_permits(
    db: AsyncSession,
    *,
    scope: Any,
    q: str | None,
    status: str | None,
    organization_id: uuid.UUID | None,
    series: str | None,
    offset: int,
    limit: int,
) -> tuple[list[Row], int]:
    """One page of permits matching `scope` and the given filters, with the
    total. INNER JOINs `organizations`, mirroring `permits.repo.list_permits`
    exactly: `Permit.organization_id` is NOT NULL, so every permit has one.

    `number` is returned as `"<series>-<number>"` (built in SQL, once, rather
    than making every caller reassemble it): `Permit.number` alone is a
    `BigInteger` and the series is what a human actually types when searching."""
    conditions: list[Any] = [scope]
    for column, value in (
        (Permit.status, status),
        (Permit.organization_id, organization_id),
        (Permit.series, series),
    ):
        if value is not None:
            conditions.append(column == value)
    display_number = (Permit.series + "-" + cast(Permit.number, String)).label("number")
    if q:
        conditions.append(_text_filter(q, display_number, Applicant.name, Applicant.phone))

    base = (
        select(
            Permit.id,
            display_number,
            Permit.status,
            Permit.organization_id,
            Applicant.name.label("applicant_name"),
            Permit.created_at,
        )
        .join(Applicant, Applicant.id == Permit.applicant_id)
        .join(Organization, Organization.id == Permit.organization_id)
        .where(*conditions)
    )
    total = (
        await db.execute(
            select(func.count()).select_from(base.with_only_columns(Permit.id).subquery())
        )
    ).scalar_one()
    order = Permit.id.desc()
    if q:
        order = _similarity_rank(q, display_number, Applicant.name).desc()
    rows = await db.execute(base.order_by(order, Permit.id.desc()).offset(offset).limit(limit))
    return list(rows.all()), total


async def create_saved_filter(db: AsyncSession, row: SavedFilter) -> SavedFilter:
    db.add(row)
    await db.flush()
    return row


async def saved_filter_by_id(db: AsyncSession, filter_id: uuid.UUID) -> SavedFilter | None:
    return await db.get(SavedFilter, filter_id)


async def list_saved_filters(
    db: AsyncSession, *, user_id: uuid.UUID, role_code: str
) -> list[SavedFilter]:
    """The caller's own profiles, plus any profile shared with their role or
    their user id explicitly (`shared = {"role_codes": [...], "user_ids":
    [...]}` — `None` means private). `?` is Postgres's jsonb "does this text
    exist as a top-level array element" operator, applied to the `role_codes`/
    `user_ids` arrays inside `shared`; a NULL `shared` makes both sides NULL,
    which the surrounding `OR` treats as not-matched rather than erroring."""
    own = SavedFilter.user_id == user_id
    shared_role = SavedFilter.shared["role_codes"].op("?")(role_code)
    shared_user = SavedFilter.shared["user_ids"].op("?")(str(user_id))
    rows = await db.execute(
        select(SavedFilter).where(or_(own, shared_role, shared_user)).order_by(SavedFilter.name)
    )
    return list(rows.scalars().all())


async def delete_saved_filter(db: AsyncSession, row: SavedFilter) -> None:
    await db.delete(row)
    await db.flush()
