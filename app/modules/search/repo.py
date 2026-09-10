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

This file, plus `saved_filters`/`export_jobs` CRUD below, is the entire read
surface: `applications`/`permits`/`applicants` are read here directly and
never written."""

import uuid
from typing import Any

from sqlalchemy import Row, String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant
from app.modules.permits.models import Permit
from app.modules.search.models import ExportJob, SavedFilter


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
    contour_organization_col: Any,
    q: str | None,
    status: str | None,
    organization_id: uuid.UUID | None,
    activity_type_id: uuid.UUID | None,
    offset: int,
    limit: int,
) -> tuple[list[Row], int]:
    """One page of applications matching `scope` and the given filters, with
    the total. `scope` is `abac.zone_filter`'s expression built by the
    service over `Organization.region_id/district_id` + the application's
    EFFECTIVE organization — `assigned_org_id` once a reviewer has taken it
    into work, else the contour's own owner (`applications.service.list_
    applications`'s own documented reasoning for why `Organization.id` is
    joined through `contour_organization_col`, never `Application.
    assigned_org_id` alone: `assigned_org_id` is null for every DRAFT and
    stays null through SUBMITTED, so scoping on it alone made a zone-scoped
    searcher unable to find their OWN leshoz's unassigned applications at
    all — the exact seam `dashboard`/`oversight` avoid by sharing this same
    `gis_service.contour_organization_column` call, seam audit 2026-09-06).

    `contour_organization_col` is built by the SERVICE (`gis_service.
    contour_organization_column`) and handed down as an expression, matching
    `applications.service.list_applications`'s own split: a repo calling
    another module's service would invert the layering even where the
    boundary rule itself is satisfied (that function's own review I2).

    LEFT JOINs `organizations` (unlike `permits.repo.list_permits`'s INNER
    JOIN): the effective-organization expression can still be NULL (an
    application naming no contour yet), and such a row must stay visible to
    a republic-wide actor (`zone_filter` returns `true()` for one, which does
    not depend on the join at all) while correctly disappearing for a
    zone-scoped one (whose `zone_filter` condition compares against a NULL
    `Organization` column through the LEFT JOIN and evaluates false)."""
    effective_org_col = func.coalesce(Application.assigned_org_id, contour_organization_col)
    conditions: list[Any] = [scope]
    for column, value in (
        (Application.status, status),
        (effective_org_col, organization_id),
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
            # Not part of `SearchResultOut` (`GET /search` never surfaced this
            # column and keeps not doing so) — used only by the export
            # renderer, which prints a human-readable organization rather
            # than a bare UUID. The LEFT JOIN above already makes it nullable.
            Organization.name.label("organization_name"),
        )
        .join(Applicant, Applicant.id == Application.applicant_id)
        .outerjoin(Organization, Organization.id == effective_org_col)
        .where(*conditions)
    )
    total = (
        await db.execute(
            select(func.count()).select_from(base.with_only_columns(Application.id).subquery())
        )
    ).scalar_one()
    # No `q`: the same order `applications.repo.list_applications` serves
    # (most recently updated first, `id DESC` as the tie-break), so the search
    # screen and the applications screen never disagree on what is on top.
    order = Application.updated_at.desc()
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
    # The number EXACTLY as the document, the permit card, every notification and
    # the public check page print it — `permits.service._permit_number`'s own
    # `f"{series} № {number:06d}"`, rendered in SQL so it is both what a result
    # row shows and what `q` is matched against (stage 7.3, finding F21).
    # Before this it was `"<series>-<number>"`, so a permit printed as
    # `А № 000003` was found by `А-3` and by nothing a person would ever type.
    # The padded form contains the bare digits, so `000003` and `3` both match
    # it as substrings — no extra clause is needed for either.
    display_number = (Permit.series + " № " + func.lpad(cast(Permit.number, String), 6, "0")).label(
        "number"
    )
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
            # Export-only, same reasoning as `search_applications` above.
            Organization.name.label("organization_name"),
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
    # No `q`: the order `permits.repo.list_permits` serves — see the
    # applications twin above.
    order = Permit.updated_at.desc()
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


# --- export_jobs (С22) ---------------------------------------------------


async def create_export_job(db: AsyncSession, row: ExportJob) -> ExportJob:
    db.add(row)
    await db.flush()
    return row


async def export_job_by_id(db: AsyncSession, job_id: uuid.UUID) -> ExportJob | None:
    return await db.get(ExportJob, job_id)


async def list_export_jobs(db: AsyncSession, *, user_id: uuid.UUID) -> list[ExportJob]:
    """The caller's OWN export history only — an export is a private working
    file, not a shared profile, so `saved_filters.shared`'s visibility rule
    does not apply here (plan header: an export is handed to someone
    OUTSIDE the screen by the operator who ran it, not browsed by peers)."""
    rows = await db.execute(
        select(ExportJob)
        .where(ExportJob.user_id == user_id)
        .order_by(ExportJob.created_at.desc(), ExportJob.id.desc())
    )
    return list(rows.scalars().all())
