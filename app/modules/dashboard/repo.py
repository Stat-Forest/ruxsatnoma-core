"""Dashboard repository. Level-5 reader (design/01 rule 5): direct read-only
`select()` access to `applications`/`permits`/`payments`/`gis`/`inspections`
tables. `dashboard` has no table of its own (design/02: "queries over the
other tables")."""

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.abac import Zone, zone_filter
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.applications.sla import SLA_ACTIVE_STATUSES
from app.modules.gis import service as gis_service
from app.modules.gis.models import Contour, ContourVersion
from app.modules.inspections.models import InspectionAct, ViolationCase
from app.modules.payments.models import BUDGET_RECIPIENT_ID, TARGET_RECIPIENT, Allocation, Invoice
from app.modules.permits.models import Permit, PermitRating


def _combined(
    actor_zone: Zone,
    filter_zone: Zone,
    *,
    region_col: Any,
    district_col: Any,
    organization_col: Any,
) -> Any:
    """The caller's own zone AND an optional explicit filter narrowing it
    further — both fail-closed the same way (`app/core/abac.py`)."""
    return and_(
        zone_filter(
            actor_zone,
            region_col=region_col,
            district_col=district_col,
            organization_col=organization_col,
        ),
        zone_filter(
            filter_zone,
            region_col=region_col,
            district_col=district_col,
            organization_col=organization_col,
        ),
    )


def _day_bounds(period_from: date, period_to: date) -> tuple[datetime, datetime]:
    """A calendar-day `[from, to]` window as an inclusive timestamptz range —
    every timestamp column here is `timestamptz`, so the boundary must be a
    moment, not a bare date (which would compare `date < timestamptz` and
    silently coerce)."""
    return datetime.combine(period_from, time.min), datetime.combine(period_to, time.max)


# --- Permits ------------------------------------------------------------------


async def permits_kpi(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    filter_zone: Zone,
    activity_type_id: uuid.UUID | None,
    period_from: date,
    period_to: date,
) -> tuple[int, int]:
    """`(issued_count, active_count)` — issued WITHIN the period (`issued_at`);
    active is a CURRENT snapshot (`status='active'`), matching the same
    "count active, not per-period" reasoning `permits.service.occupancy_
    provider`/`load_provider` already use."""
    zone_clause = _combined(
        actor_zone,
        filter_zone,
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Organization.id,
    )
    base = select(Permit).join(Organization, Organization.id == Permit.organization_id)
    conditions: list[Any] = [zone_clause]
    if activity_type_id is not None:
        conditions.append(Permit.activity_type_id == activity_type_id)
    start, end = _day_bounds(period_from, period_to)
    issued = (
        await db.execute(
            select(func.count()).select_from(
                base.where(*conditions, Permit.issued_at.between(start, end)).subquery()
            )
        )
    ).scalar_one()
    active = (
        await db.execute(
            select(func.count()).select_from(
                base.where(*conditions, Permit.status == "active").subquery()
            )
        )
    ).scalar_one()
    return issued, active


async def sb_load_total(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    filter_zone: Zone,
    activity_type_id: uuid.UUID | None,
) -> Decimal:
    """SUM of `permits.sb_load` for currently ACTIVE permits — the same figure
    `norms.service.LOAD_PROVIDERS`' registered `permits.service.load_provider`
    computes per contour, aggregated here across a whole territory slice
    instead of one contour at a time."""
    zone_clause = _combined(
        actor_zone,
        filter_zone,
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Organization.id,
    )
    conditions: list[Any] = [zone_clause, Permit.status == "active"]
    if activity_type_id is not None:
        conditions.append(Permit.activity_type_id == activity_type_id)
    stmt = (
        select(func.coalesce(func.sum(Permit.sb_load), 0))
        .select_from(Permit)
        .join(Organization, Organization.id == Permit.organization_id)
        .where(*conditions)
    )
    value = (await db.execute(stmt)).scalar_one()
    # `coalesce(..., 0)` guarantees a non-NULL result at the SQL level;
    # pyright cannot see that through a raw `func.coalesce()` expression.
    assert value is not None
    return value


# --- Satisfaction (ratings, Task 6, ruling #143) -------------------------


async def satisfaction_kpi(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    filter_zone: Zone,
    activity_type_id: uuid.UUID | None,
    period_from: date,
    period_to: date,
) -> tuple[Decimal | None, int]:
    """`(avg_score, count)` over `permit_ratings` in scope.

    The period applies to `PermitRating.created_at` — when the citizen
    rated — never `Permit.issued_at`: a rating left this month counts this
    month, regardless of when the underlying permit was issued.

    Same `_combined(...)` three-axis zone clause as `permits_kpi` — region,
    district AND organization — for the same reason: narrowing to
    `organization_id` alone would pass a region-or-district-scoped actor
    for every organization in the country.

    `ROUND(AVG(score), 2)` runs in SQL, never in Python, matching the
    sibling `permits.repo.ratings_overall`: `AVG` over zero rows is SQL
    `NULL`, and `ROUND(NULL, 2)` stays `NULL`, so an empty period reads as
    `(None, 0)` rather than a division by zero or an invented `0` — a
    portal may not state a number it cannot produce.
    """
    zone_clause = _combined(
        actor_zone,
        filter_zone,
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Organization.id,
    )
    conditions: list[Any] = [
        zone_clause,
        PermitRating.created_at.between(*_day_bounds(period_from, period_to)),
    ]
    if activity_type_id is not None:
        conditions.append(Permit.activity_type_id == activity_type_id)
    stmt = (
        select(func.round(func.avg(PermitRating.score), 2), func.count())
        .select_from(PermitRating)
        .join(Permit, Permit.id == PermitRating.permit_id)
        .join(Organization, Organization.id == Permit.organization_id)
        .where(*conditions)
    )
    avg_score, count = (await db.execute(stmt)).one()
    return avg_score, count


# --- Applications ---------------------------------------------------------


def _application_org_column() -> Any:
    return func.coalesce(
        Application.assigned_org_id,
        gis_service.contour_organization_column(Application.contour_id),
    )


async def applications_by_status(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    filter_zone: Zone,
    activity_type_id: uuid.UUID | None,
    period_from: date,
    period_to: date,
) -> dict[str, int]:
    """Counted by `submitted_at` — a DRAFT never submitted contributes to no
    period (`tz/04` С21's own KPI list is about the processed pipeline, not
    autosaved drafts)."""
    org_col = _application_org_column()
    zone_clause = _combined(
        actor_zone,
        filter_zone,
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Organization.id,
    )
    start, end = _day_bounds(period_from, period_to)
    conditions: list[Any] = [zone_clause, Application.submitted_at.between(start, end)]
    if activity_type_id is not None:
        conditions.append(Application.activity_type_id == activity_type_id)
    stmt = (
        select(Application.status, func.count())
        .join(Organization, Organization.id == org_col)
        .where(*conditions)
        .group_by(Application.status)
    )
    return {row[0]: row[1] for row in (await db.execute(stmt)).all()}


async def sla_kpi(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    filter_zone: Zone,
    activity_type_id: uuid.UUID | None,
    now: datetime,
) -> tuple[int, int]:
    """`(active_count, overdue_count)` — a CURRENT snapshot of the SLA clock,
    the same predicate `applications.sla.is_overdue` reads
    (`SLA_ACTIVE_STATUSES`, `sla_deadline_at`), reused rather than
    re-derived (lesson: an enum-ish/derived vocabulary has one source)."""
    org_col = _application_org_column()
    zone_clause = _combined(
        actor_zone,
        filter_zone,
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Organization.id,
    )
    conditions: list[Any] = [zone_clause, Application.status.in_(SLA_ACTIVE_STATUSES)]
    if activity_type_id is not None:
        conditions.append(Application.activity_type_id == activity_type_id)
    base = select(Application).join(Organization, Organization.id == org_col).where(*conditions)
    active = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    overdue_stmt = base.where(
        Application.sla_deadline_at.is_not(None), Application.sla_deadline_at < now
    )
    overdue = (
        await db.execute(select(func.count()).select_from(overdue_stmt.subquery()))
    ).scalar_one()
    return active, overdue


async def rejection_reasons(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    filter_zone: Zone,
    activity_type_id: uuid.UUID | None,
    period_from: date,
    period_to: date,
) -> list[tuple[uuid.UUID, int]]:
    org_col = _application_org_column()
    zone_clause = _combined(
        actor_zone,
        filter_zone,
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Organization.id,
    )
    start, end = _day_bounds(period_from, period_to)
    conditions: list[Any] = [
        zone_clause,
        Application.status == "REJECTED",
        Application.rejection_reason_item_id.is_not(None),
        Application.decided_at.between(start, end),
    ]
    if activity_type_id is not None:
        conditions.append(Application.activity_type_id == activity_type_id)
    stmt = (
        select(Application.rejection_reason_item_id, func.count())
        .join(Organization, Organization.id == org_col)
        .where(*conditions)
        .group_by(Application.rejection_reason_item_id)
    )
    return [(row[0], row[1]) for row in (await db.execute(stmt)).all()]


# --- Payments ---------------------------------------------------------------


async def payments_kpi(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    filter_zone: Zone,
    period_from: date,
    period_to: date,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """`(invoiced_amount, paid_amount, budget_share, recipient_share)`.

    **`budget_share` changed meaning on 2026-09-09 (decision #154, stage
    7.9 task 8, Override 1).** Stage 7.9 replaced the fixed 50/50 split
    with a configurable receivers directory and migration `0046` removed
    `'budget'` from `allocations.target_valid` entirely — reading
    `target == "budget"` here (this function's own shape until that task)
    silently summed to ZERO on every fresh database, with no test failing,
    exactly the hiding-direction defect this project keeps producing. A
    single `budget_share` field is a question with no answer once the
    split is an arbitrary-size directory — there is no longer "the
    budget's half" — so this reads it the one way that still has an
    honest meaning: the seeded budget recipient's OWN share, by
    `recipient_id == BUDGET_RECIPIENT_ID` (decision #157's default
    directory entry), never by a `target` string. `recipient_share` stays
    a `target` read: the leshoz's own remainder is unambiguously
    `target=TARGET_RECIPIENT` no matter how many receivers are configured
    beside it."""
    org_col = _application_org_column()
    zone_clause = _combined(
        actor_zone,
        filter_zone,
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Organization.id,
    )
    start, end = _day_bounds(period_from, period_to)
    invoice_ids = (
        select(Invoice.id)
        .join(Application, Application.id == Invoice.application_id)
        .join(Organization, Organization.id == org_col)
        .where(zone_clause, Invoice.issued_at.between(start, end))
    )
    invoiced = (
        await db.execute(
            select(func.coalesce(func.sum(Invoice.amount), 0)).where(Invoice.id.in_(invoice_ids))
        )
    ).scalar_one()
    paid = (
        await db.execute(
            select(func.coalesce(func.sum(Invoice.amount), 0)).where(
                Invoice.id.in_(invoice_ids), Invoice.status == "paid"
            )
        )
    ).scalar_one()
    recipient_share = (
        await db.execute(
            select(func.coalesce(func.sum(Allocation.amount), 0)).where(
                Allocation.invoice_id.in_(invoice_ids), Allocation.target == TARGET_RECIPIENT
            )
        )
    ).scalar_one()
    budget_share = (
        await db.execute(
            select(func.coalesce(func.sum(Allocation.amount), 0)).where(
                Allocation.invoice_id.in_(invoice_ids),
                Allocation.recipient_id == BUDGET_RECIPIENT_ID,
            )
        )
    ).scalar_one()
    return (invoiced, paid, budget_share, recipient_share)


# --- Occupancy (gis) ----------------------------------------------------------


async def published_contours_in_scope(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    filter_zone: Zone,
) -> list[tuple[uuid.UUID, Decimal]]:
    """`[(contour_id, area_ha)]` for every PUBLISHED contour version whose
    contour is inside scope — `Contour.organization_id` is direct, no
    coalesce/fallback needed here (unlike `applications`)."""
    zone_clause = _combined(
        actor_zone,
        filter_zone,
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Organization.id,
    )
    stmt = (
        select(Contour.id, ContourVersion.area_ha)
        .join(ContourVersion, ContourVersion.contour_id == Contour.id)
        .join(Organization, Organization.id == Contour.organization_id)
        .where(zone_clause, ContourVersion.status == "published")
    )
    return [(row[0], row[1]) for row in (await db.execute(stmt)).all()]


# --- Inspections (4.1) --------------------------------------------------------


async def inspections_kpi(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    filter_zone: Zone,
    period_from: date,
    period_to: date,
) -> tuple[int, int]:
    """`(inspections_count, violations_count)` — signed field acts occurring
    in the period, and the violation cases those SAME acts opened, both
    zone-scoped.

    Both counts are filtered by the ACT's `occurred_at` (the real-world visit
    date), never `violation_cases.created_at` (the row's own insert time) —
    consistent with `inspections_count`'s own period column, and correct for
    the same reason `permits_kpi` filters by `issued_at`, not by whatever
    moment a later admin action happened to touch the row: an act signed a
    few days after the visit it records must count toward the period the
    VIOLATION occurred in, not the period the paperwork was filed in.

    Outer join, not `payments_kpi`'s inner one: `inspection_acts.
    organization_id`/`violation_cases.organization_id` are nullable (an
    "activity without a permit" patrol act may resolve to no organization at
    all — `inspections/models.py`'s own docstring), so an inner join would
    silently drop those rows from every viewer's count, zone-scoped or not.
    `zone_filter` still excludes them for a zone-scoped viewer on its own
    (a NULL joined column fails every equality test), which is the same
    fail-closed posture `search`'s own `outerjoin(Organization, ...)` on
    `applications.assigned_org_id` already uses.

    No `activity_type_id` parameter, matching `payments_kpi`/
    `published_contours_in_scope` above: an inspection act's activity type
    would have to be resolved through whichever of `permit_id`/
    `application_id` it names (and neither, for a bare patrol), which is a
    second cross-module resolution this tile does not need to invent."""
    start, end = _day_bounds(period_from, period_to)

    act_zone_clause = _combined(
        actor_zone,
        filter_zone,
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=InspectionAct.organization_id,
    )
    inspections_count = (
        await db.execute(
            select(func.count()).select_from(
                select(InspectionAct.id)
                .outerjoin(Organization, Organization.id == InspectionAct.organization_id)
                .where(
                    act_zone_clause,
                    InspectionAct.status == "signed",
                    InspectionAct.occurred_at.between(start, end),
                )
                .subquery()
            )
        )
    ).scalar_one()

    case_zone_clause = _combined(
        actor_zone,
        filter_zone,
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=ViolationCase.organization_id,
    )
    violations_count = (
        await db.execute(
            select(func.count()).select_from(
                select(ViolationCase.id)
                .outerjoin(Organization, Organization.id == ViolationCase.organization_id)
                .join(InspectionAct, InspectionAct.id == ViolationCase.act_id)
                .where(
                    case_zone_clause,
                    InspectionAct.occurred_at.between(start, end),
                )
                .subquery()
            )
        )
    ).scalar_one()
    return inspections_count, violations_count


# --- Territory-slice drill-down (k-anonymity) --------------------------------


async def slice_by_region(
    db: AsyncSession, *, actor_zone: Zone, period_from: date, period_to: date
) -> list[dict[str, Any]]:
    return await _slice(
        db,
        actor_zone=actor_zone,
        group_col=Organization.region_id,
        period_from=period_from,
        period_to=period_to,
    )


async def slice_by_district(
    db: AsyncSession, *, actor_zone: Zone, region_id: uuid.UUID, period_from: date, period_to: date
) -> list[dict[str, Any]]:
    return await _slice(
        db,
        actor_zone=actor_zone,
        group_col=Organization.district_id,
        period_from=period_from,
        period_to=period_to,
        extra_where=[Organization.region_id == region_id],
    )


async def slice_by_organization(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    district_id: uuid.UUID,
    period_from: date,
    period_to: date,
) -> list[dict[str, Any]]:
    return await _slice(
        db,
        actor_zone=actor_zone,
        group_col=Organization.id,
        period_from=period_from,
        period_to=period_to,
        extra_where=[Organization.district_id == district_id],
    )


async def slice_by_contour(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    organization_id: uuid.UUID,
    period_from: date,
    period_to: date,
) -> list[dict[str, Any]]:
    """The finest granularity — where k-anonymity almost always bites (a
    contour usually has very few distinct applicants)."""
    zone_clause = zone_filter(
        actor_zone,
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Organization.id,
    )
    start, end = _day_bounds(period_from, period_to)
    stmt = (
        select(
            Application.contour_id,
            func.count(func.distinct(Application.applicant_id)),
            func.count(Application.id),
        )
        .join(Organization, Organization.id == _application_org_column())
        .where(
            zone_clause,
            Organization.id == organization_id,
            Application.contour_id.is_not(None),
            Application.submitted_at.between(start, end),
        )
        .group_by(Application.contour_id)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "key": row[0],
            "label": str(row[0]),
            "applicant_count": row[1],
            "applications_count": row[2],
            "permits_count": None,
        }
        for row in rows
    ]


async def _slice(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    group_col: Any,
    period_from: date,
    period_to: date,
    extra_where: list[Any] | None = None,
) -> list[dict[str, Any]]:
    zone_clause = zone_filter(
        actor_zone,
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Organization.id,
    )
    start, end = _day_bounds(period_from, period_to)
    conditions: list[Any] = [zone_clause, *(extra_where or [])]
    stmt = (
        select(
            group_col,
            func.count(func.distinct(Application.applicant_id)),
            func.count(Application.id),
        )
        .select_from(Application)
        .join(Organization, Organization.id == _application_org_column())
        .where(*conditions, Application.submitted_at.between(start, end))
        .group_by(group_col)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "key": row[0],
            "label": str(row[0]),
            "applicant_count": row[1],
            "applications_count": row[2],
            "permits_count": None,
        }
        for row in rows
        if row[0] is not None
    ]
