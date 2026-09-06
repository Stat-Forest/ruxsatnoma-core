"""Oversight repository. Level-5 reader (design/01 rule 5): direct read-only
`select()` access to any table is allowed here — `applications`, `permits`,
`payments`, `gis`, `inspections` and `reports` models are imported for SELECT
only, never for a write and never re-exported for another module to import
from here."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.abac import Zone, zone_filter
from app.modules.admin.models import Organization
from app.modules.applications.models import Application, ApplicationStatusHistory
from app.modules.audit.models import AuditLog
from app.modules.gis import service as gis_service
from app.modules.inspections.models import InspectionAct
from app.modules.oversight.models import OversightEvent, RiskIndicator
from app.modules.payments.models import Invoice, Refund
from app.modules.permits.models import Permit, PermitStatusHistory
from app.modules.reports.models import Report


async def insert_event(
    db: AsyncSession,
    *,
    event_type: str,
    object_type: str | None,
    object_id: uuid.UUID | None,
    payload: dict[str, Any] | None,
    correlation_id: str | None,
) -> OversightEvent:
    row = OversightEvent(
        event_type=event_type,
        object_type=object_type,
        object_id=object_id,
        payload=payload,
        correlation_id=correlation_id,
    )
    db.add(row)
    await db.flush()
    return row


async def insert_risk_indicator_if_new(
    db: AsyncSession,
    *,
    code: str,
    level: str,
    object_type: str | None,
    object_id: uuid.UUID | None,
    description: str,
    details: dict[str, Any] | None,
    idempotency_key: uuid.UUID,
    occurred_at: datetime | None = None,
) -> bool:
    """`INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`, reporting whether
    a row was actually written. A native upsert rather than
    try/insert/except IntegrityError: this is called once per candidate inside
    a batch loop (`service.harvest`/`service.sweep_overlapping_permits`), and a
    single statement per candidate — success or silent skip — needs no
    SAVEPOINT around it (the lesson on a failed statement aborting the whole
    transaction does not apply to a conflict `DO NOTHING` never raises)."""
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "code": code,
        "level": level,
        "object_type": object_type,
        "object_id": object_id,
        "description": description,
        "details": details,
        "idempotency_key": idempotency_key,
    }
    if occurred_at is not None:
        values["occurred_at"] = occurred_at
    stmt = (
        pg_insert(RiskIndicator)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[RiskIndicator.idempotency_key])
    )
    result = await db.execute(stmt)
    # `.rowcount` is a real int at runtime for a Core INSERT executed through
    # AsyncSession (asyncpg's CursorResult) — the async stubs just type
    # execute()'s return as the base Result, which doesn't declare it
    # (lesson, `app/workers/jobs.py::purge_stale_rows`'s own precedent).
    return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue]


# --- Harvesting already-tagged audit_log rows --------------------------------

# `extra ? 'risk_indicator'` — PostgreSQL's `?` JSONB "has key" operator.
# Deliberately no index added to `audit_log` (owned by `audit`, level 0):
# adding one would need an ORM mirror in `audit.models.AuditLog` (the
# autogenerate-diff guard), a cross-module reach this reader avoids — a plain
# sequential scan is cheap at today's volumes. RI-04 (norms/tariffs
# retroactive publish) is not tagged the same way (see `service.py`), so it
# needs a second predicate — low volume (publish actions only) either way.
_RISK_TAG = text("extra ? 'risk_indicator'")
_RETROACTIVE_PUBLISH = text("action LIKE '%.publish' AND new_value ->> 'retroactive' = 'true'")


async def harvest_candidates(db: AsyncSession, *, limit: int) -> list[AuditLog]:
    """`audit_log` rows carrying a risk-indicator signal that has no matching
    `risk_indicators` row yet, oldest first (`id` is a monotonic UUIDv7, so
    ordering by it orders by time without a separate column)."""
    already = select(RiskIndicator.idempotency_key)
    stmt = (
        select(AuditLog)
        .where(
            or_(_RISK_TAG, _RETROACTIVE_PUBLISH),
            AuditLog.id.not_in(already),
        )
        .order_by(AuditLog.id)
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


# --- RI-03: overlapping active permits on one contour ------------------------


async def overlapping_active_permit_pairs(db: AsyncSession) -> list[tuple[Permit, Permit]]:
    """Every pair of DISTINCT permits sharing a contour, both currently
    `active`/`suspended` (design/02's own reasoning for `_RI_10_PERMIT_
    STATUSES` in `payments.repo` applies here too: `revoked`/`expired`/
    `archived` are already dealt with, not a live double-booking), whose
    `[period_from, period_to]` ranges overlap. Self-joined once, ids compared
    to report each pair exactly once (`a.id < b.id`)."""
    a = Permit.__table__.alias("permit_a")
    b = Permit.__table__.alias("permit_b")
    active = ("active", "suspended")
    # Every column labelled per side (`a_id`, `b_id`, ...): selecting both
    # aliases' identically-named columns unlabelled would make `Row` attribute
    # access ambiguous (the second silently shadows the first in `_mapping`).
    a_cols = [col.label(f"a_{col.name}") for col in a.c]
    b_cols = [col.label(f"b_{col.name}") for col in b.c]
    stmt = (
        select(*a_cols, *b_cols)
        .select_from(a.join(b, a.c.contour_id == b.c.contour_id))
        .where(
            a.c.id < b.c.id,
            a.c.status.in_(active),
            b.c.status.in_(active),
            a.c.period_from <= b.c.period_to,
            a.c.period_to >= b.c.period_from,
        )
    )
    rows = (await db.execute(stmt)).all()
    return [(_permit_from_row(row, "a_"), _permit_from_row(row, "b_")) for row in rows]


def _permit_from_row(row: Any, prefix: str) -> Permit:
    """Build a transient (non-session) `Permit` from one labelled half of the
    aliased self-join row — read-only reporting data, never added to the
    session."""
    mapping = row._mapping
    permit = Permit()
    for col in Permit.__table__.c:
        setattr(permit, col.name, mapping[f"{prefix}{col.name}"])
    return permit


# --- Zone resolution for a risk_indicators/oversight_events row --------------

# Only these object types have a knowable per-leshoz owner today (ruling f in
# the plan). Anything else (a tariff, a rule_parameter, a certificate...) has
# no natural organization and is visible only to a republic-wide viewer.
_APPLICATION_ORG = select(
    func.coalesce(
        Application.assigned_org_id,
        gis_service.contour_organization_column(Application.contour_id),
    )
).where(Application.id == RiskIndicator.object_id)

_PERMIT_ORG = select(Permit.organization_id).where(Permit.id == RiskIndicator.object_id)

_INVOICE_ORG = (
    select(
        func.coalesce(
            Application.assigned_org_id,
            gis_service.contour_organization_column(Application.contour_id),
        )
    )
    .select_from(Invoice)
    .join(Application, Application.id == Invoice.application_id)
    .where(Invoice.id == RiskIndicator.object_id)
)

_REFUND_ORG = (
    select(
        func.coalesce(
            Application.assigned_org_id,
            gis_service.contour_organization_column(Application.contour_id),
        )
    )
    .select_from(Refund)
    .join(Application, Application.id == Refund.application_id)
    .where(Refund.id == RiskIndicator.object_id)
)

# `signatures.service.sign()` raises RI-05 under whichever `object_type` ITS
# caller passed — the four below (seam audit, 2026-09-06: the original four
# above pre-date `inspections`/`reports`, both level-5 siblings built in the
# same parallel wave that could not see this file). Each DOES have a knowable
# per-leshoz owner — `inspection_acts.organization_id`/`reports.
# organization_id` are direct columns, not "no natural organization" the way
# a `tariff`/`rule_parameter`/bare `certificate` genuinely has none of — so
# leaving them unmapped hid RI-05 (and any future RI code tagged the same
# way) from every zone-scoped viewer, on exactly the object types a
# leshoz-scoped prosecutor or head most needs to see it for: their own
# inspector's act, their own head's decision, their own submission, their
# own report.
_INSPECTION_ACT_ORG = select(InspectionAct.organization_id).where(
    InspectionAct.id == RiskIndicator.object_id
)
_REPORT_ORG = select(Report.organization_id).where(Report.id == RiskIndicator.object_id)
_APPLICATION_SUBMISSION_ORG = (
    select(
        func.coalesce(
            Application.assigned_org_id,
            gis_service.contour_organization_column(Application.contour_id),
        )
    )
    .select_from(ApplicationStatusHistory)
    .join(Application, Application.id == ApplicationStatusHistory.application_id)
    .where(ApplicationStatusHistory.id == RiskIndicator.object_id)
)
_PERMIT_DECISION_ORG = (
    select(Permit.organization_id)
    .select_from(PermitStatusHistory)
    .join(Permit, Permit.id == PermitStatusHistory.permit_id)
    .where(PermitStatusHistory.id == RiskIndicator.object_id)
)


def _resolved_organization_id() -> Any:
    """One SQL expression naming the organization a `risk_indicators` row
    belongs to, or NULL when `object_type` has no per-leshoz owner."""
    return case(
        (RiskIndicator.object_type == "permit", _PERMIT_ORG.scalar_subquery()),
        (RiskIndicator.object_type == "application", _APPLICATION_ORG.scalar_subquery()),
        (RiskIndicator.object_type == "invoice", _INVOICE_ORG.scalar_subquery()),
        (RiskIndicator.object_type == "refund", _REFUND_ORG.scalar_subquery()),
        (RiskIndicator.object_type == "inspection_act", _INSPECTION_ACT_ORG.scalar_subquery()),
        (RiskIndicator.object_type == "report", _REPORT_ORG.scalar_subquery()),
        (
            RiskIndicator.object_type == "application_submission",
            _APPLICATION_SUBMISSION_ORG.scalar_subquery(),
        ),
        (
            RiskIndicator.object_type == "permit_decision",
            _PERMIT_DECISION_ORG.scalar_subquery(),
        ),
        else_=None,
    )


def zone_visible_clause(zone: Zone) -> Any:
    """`True` for a republic-wide zone; otherwise TRUE only when the row's
    resolved organization is inside `zone` — fail-closed for an object type
    with no resolvable organization at all, matching `zone_filter`'s own
    posture rather than guessing an owner."""
    if zone == Zone(None, None, None):
        return text("true")
    resolved = _resolved_organization_id()
    return and_(
        resolved.is_not(None),
        resolved.in_(
            select(Organization.id).where(
                zone_filter(
                    zone,
                    region_col=Organization.region_id,
                    district_col=Organization.district_id,
                    organization_col=Organization.id,
                )
            )
        ),
    )


async def list_risk_indicators(
    db: AsyncSession,
    *,
    zone: Zone,
    code: str | None,
    level: str | None,
    status: str | None,
    object_type: str | None,
    object_id: uuid.UUID | None,
    period_from: date | None,
    period_to: date | None,
    offset: int,
    limit: int,
) -> tuple[list[RiskIndicator], int]:
    conditions = [zone_visible_clause(zone)]
    if code is not None:
        conditions.append(RiskIndicator.code == code)
    if level is not None:
        conditions.append(RiskIndicator.level == level)
    if status is not None:
        conditions.append(RiskIndicator.status == status)
    if object_type is not None:
        conditions.append(RiskIndicator.object_type == object_type)
    if object_id is not None:
        conditions.append(RiskIndicator.object_id == object_id)
    if period_from is not None:
        conditions.append(func.date(RiskIndicator.occurred_at) >= period_from)
    if period_to is not None:
        conditions.append(func.date(RiskIndicator.occurred_at) <= period_to)
    where = and_(*conditions)
    total = (
        await db.execute(select(func.count()).select_from(RiskIndicator).where(where))
    ).scalar_one()
    stmt = (
        select(RiskIndicator)
        .where(where)
        .order_by(RiskIndicator.occurred_at.desc(), RiskIndicator.id.desc())
        .offset(offset)
        .limit(limit)
    )
    items = list((await db.execute(stmt)).scalars().all())
    return items, total


async def count_risk_indicators_by(
    db: AsyncSession,
    *,
    zone: Zone,
    period_from: date | None,
    period_to: date | None,
) -> list[tuple[str, str, int]]:
    """`(code, level, count)` rows for the dashboard's risk-indicator tile —
    zone-scoped the same way `list_risk_indicators` is (ruling f), so a
    zone-scoped viewer's dashboard never contradicts what their own
    `GET /oversight/risk-indicators` page would show."""
    conditions = [zone_visible_clause(zone)]
    if period_from is not None:
        conditions.append(func.date(RiskIndicator.occurred_at) >= period_from)
    if period_to is not None:
        conditions.append(func.date(RiskIndicator.occurred_at) <= period_to)
    stmt = (
        select(RiskIndicator.code, RiskIndicator.level, func.count())
        .where(and_(*conditions))
        .group_by(RiskIndicator.code, RiskIndicator.level)
    )
    return [(row[0], row[1], row[2]) for row in (await db.execute(stmt)).all()]


async def list_events(
    db: AsyncSession,
    *,
    event_type: str | None,
    object_type: str | None,
    period_from: date | None,
    period_to: date | None,
    offset: int,
    limit: int,
) -> tuple[list[OversightEvent], int]:
    """No zone predicate: `oversight_events` is the raw RN-bound stream, not a
    case register — its own consumer (once RN is connected) reads the whole
    stream. `oversight.view` already gates who reaches this route at all."""
    conditions = []
    if event_type is not None:
        conditions.append(OversightEvent.event_type == event_type)
    if object_type is not None:
        conditions.append(OversightEvent.object_type == object_type)
    if period_from is not None:
        conditions.append(func.date(OversightEvent.occurred_at) >= period_from)
    if period_to is not None:
        conditions.append(func.date(OversightEvent.occurred_at) <= period_to)
    where = and_(*conditions) if conditions else text("true")
    total = (
        await db.execute(select(func.count()).select_from(OversightEvent).where(where))
    ).scalar_one()
    stmt = (
        select(OversightEvent)
        .where(where)
        .order_by(OversightEvent.occurred_at.desc(), OversightEvent.id.desc())
        .offset(offset)
        .limit(limit)
    )
    items = list((await db.execute(stmt)).scalars().all())
    return items, total
