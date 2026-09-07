"""Oversight service — the module's only door for everyone else (design/01
rule 1). Two jobs live here: (1) `record_event`, the bus subscriber that turns
the five events `applications`/`payments` already publish into the
accumulating `oversight_events` stream, and (2) the RI harvester/detector
(`harvest`, `sweep_overlapping_permits`, `sweep_long_active_without_
inspection`) — see `models.py`'s module docstring for why most codes are a
HARVEST of an existing tag, not a fresh detector.

The read surface (`list_risk_indicators`/`list_events`) is С22's prosecutor
window: zone-scoped (`app/core/abac.py::zone_filter`, fails closed) and
audited on every call (`tz/03`: "каждый просмотр/поиск/экспорт — в аудит")."""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.abac import zone_of
from app.core.events import Event
from app.core.schemas import PageParams
from app.modules.audit import service as audit
from app.modules.audit.models import AuditLog
from app.modules.auth.models import User
from app.modules.oversight import repo
from app.modules.oversight.models import (
    RI_LEVEL_BY_CODE,
    RISK_INDICATOR_CODES,
    OversightEvent,
    RiskIndicator,
)

# Deterministic namespace for RI-03's synthesized idempotency key (there is no
# source `audit_log` row to key off, unlike every harvested code) — fixed and
# arbitrary, the same role a hard-coded UUID constant plays elsewhere in this
# codebase (e.g. `permits`' own fixed template ids).
_RI03_NAMESPACE = uuid.UUID("0198f000-0000-7000-8000-0000000000ff")

# RI-14's own namespace, same role and same reasoning as RI-03's above — a
# permit either does or does not qualify at sweep time, so keying on the
# permit's own id (rather than a source audit_log row, which does not exist
# for a direct detector) is what makes a re-run idempotent.
_RI14_NAMESPACE = uuid.UUID("0198f000-0000-7000-8000-0000000000fe")

# Ruling #104: RI-14 "long active with no inspection" — the setting
# `sweep_long_active_without_inspection` reads each run (60s cache,
# `settings_store`'s own TTL), never a module constant, so the threshold can
# be tuned without a deploy.
RI14_THRESHOLD_SETTING = "oversight_ri14_no_inspection_days"

OVERSIGHT_VIEW_ACTION = "oversight.view"


# --- Event-bus subscriber: the accumulating oversight_events stream ----------


async def record_event(db: AsyncSession, event: Event) -> None:
    """Subscribed (in `app/event_subscriptions.py`) to the five bus events
    `applications`/`payments` already publish. Writes ONE `oversight_events`
    row per event, inside the publisher's own transaction (design/01 rule 4)
    — no other module is modified to produce this stream."""
    object_type, object_id = _event_object(event)
    await repo.insert_event(
        db,
        event_type=event.name,
        object_type=object_type,
        object_id=object_id,
        payload=_jsonable(dict(event.payload)),
        correlation_id=None,
    )


def _jsonable(value: Any) -> Any:
    """Nothing in this app configures a JSON encoder (lesson): coerce before
    every JSON boundary. `applications.events`' own payload carries a raw
    `uuid.UUID` — unlike `payments.events.PAYMENT_CONFIRMED`, whose docstring
    promises strings — so a bare `dict(event.payload)` into a JSONB column
    raises `TypeError` at flush. Recurses through `dict`/`list`; every other
    value passes through unchanged (today's payloads are flat, but a future
    one is not this function's business to assume)."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _event_object(event: Event) -> tuple[str | None, uuid.UUID | None]:
    """`applications.events`' four names carry only `application_id`;
    `payments.events.PAYMENT_CONFIRMED` carries `invoice_id` AND
    `application_id` (both strings — `applications/events.py`'s own docstring
    on why neither event payload ever carries anything beyond identifiers)."""
    payload = event.payload
    if "invoice_id" in payload:
        return "invoice", uuid.UUID(str(payload["invoice_id"]))
    if "application_id" in payload:
        return "application", uuid.UUID(str(payload["application_id"]))
    return None, None


# --- RI harvest + the one genuine detector (RI-03) ---------------------------


def _map_audit_row(row: AuditLog) -> tuple[str, str, dict[str, Any]] | None:
    """`None` when the row matched the SQL candidate filter but turns out not
    to carry a recognised signal after all (defensive — the DB-level filter is
    intentionally loose, see `repo.harvest_candidates`)."""
    extra = row.extra or {}
    tag = extra.get("risk_indicator")
    if isinstance(tag, str) and tag in RISK_INDICATOR_CODES:
        details: dict[str, Any] = {"action": row.action, "result": row.result}
        if row.basis:
            details["basis"] = row.basis
        reason = extra.get("reason")
        if reason:
            details["reason"] = reason
        description = f"{row.action}: {row.basis or reason or 'flagged'}"
        return tag, description, details
    new_value = row.new_value or {}
    if row.action.endswith(".publish") and new_value.get("retroactive") is True:
        details = {"action": row.action, "new_value": new_value}
        description = f"{row.action}: effective date is in the past"
        return "RI-04", description, details
    return None


async def harvest(db: AsyncSession, *, batch_size: int = 500) -> int:
    """Convert already-tagged `audit_log` rows into `risk_indicators` rows.
    Idempotent by construction (`repo.insert_risk_indicator_if_new` keys on
    the source row's own id) — safe to call on any schedule, any number of
    times, including concurrently from more than one process."""
    candidates = await repo.harvest_candidates(db, limit=batch_size)
    written = 0
    for row in candidates:
        mapped = _map_audit_row(row)
        if mapped is None:
            continue
        code, description, details = mapped
        ok = await repo.insert_risk_indicator_if_new(
            db,
            code=code,
            level=RI_LEVEL_BY_CODE[code],
            object_type=row.object_type,
            object_id=row.object_id,
            description=description,
            details=details,
            idempotency_key=row.id,
            occurred_at=row.occurred_at,
        )
        if ok:
            written += 1
    return written


async def sweep_overlapping_permits(db: AsyncSession) -> int:
    """RI-03: no existing tag anywhere raises this (grepped before writing
    this module — see the plan's ruling a) — this is the one code this module
    detects directly rather than harvests. `object_type="permit"` (not
    "contour"): only `permit`/`application`/`invoice`/`refund` resolve to an
    organization for zone scoping (`repo._resolved_organization_id`), and
    both permits in an overlapping pair share the same contour's organization
    either way."""
    pairs = await repo.overlapping_active_permit_pairs(db)
    written = 0
    for permit_a, permit_b in pairs:
        key = uuid.uuid5(_RI03_NAMESPACE, ":".join(sorted((str(permit_a.id), str(permit_b.id)))))
        details = {
            "permit_a": str(permit_a.id),
            "permit_b": str(permit_b.id),
            "contour_id": str(permit_a.contour_id),
            "period_a": [permit_a.period_from.isoformat(), permit_a.period_to.isoformat()],
            "period_b": [permit_b.period_from.isoformat(), permit_b.period_to.isoformat()],
        }
        description = (
            f"Permits {permit_a.id} and {permit_b.id} are both "
            f"{permit_a.status}/{permit_b.status} on contour {permit_a.contour_id} "
            "with overlapping periods"
        )
        ok = await repo.insert_risk_indicator_if_new(
            db,
            code="RI-03",
            level=RI_LEVEL_BY_CODE["RI-03"],
            object_type="permit",
            object_id=permit_b.id,
            description=description,
            details=details,
            idempotency_key=key,
        )
        if ok:
            written += 1
    return written


async def sweep_long_active_without_inspection(db: AsyncSession) -> int:
    """RI-14 (ruling #104): an `active` permit running `oversight_ri14_no_
    inspection_days` (default 30 — the strictest of three options Oybek was
    offered) with no `inspection_acts` row at all. No existing tag raises this
    either, the same reasoning `sweep_overlapping_permits` gives for RI-03: a
    direct detector, not a harvest.

    The threshold is read fresh every sweep (`settings_store.get_int`, 60s
    cache) rather than frozen into a constant — ruling #104's own text warns
    that 30 fires often and an indicator that always fires stops being read,
    so this has to be tunable without a deploy once real inspection volume
    can judge it.

    Idempotent the same way RI-03 is: `idempotency_key` is a deterministic
    `uuid5` of the permit's own id (there is no source `audit_log` row for a
    direct detector to key off), so a permit already carrying an RI-14 row
    is skipped on every later sweep — it does not re-fire, and it is not
    cleared if an inspection arrives afterwards; the row is history, not a
    live flag."""
    threshold_days = await settings_store.get_int(db, RI14_THRESHOLD_SETTING)
    cutoff = datetime.now(UTC) - timedelta(days=threshold_days)
    permits = await repo.long_active_permits_without_inspection(db, cutoff=cutoff)
    written = 0
    for permit in permits:
        key = uuid.uuid5(_RI14_NAMESPACE, str(permit.id))
        details = {
            "permit_id": str(permit.id),
            "issued_at": permit.issued_at.isoformat() if permit.issued_at else None,
            "threshold_days": threshold_days,
        }
        description = (
            f"Permit {permit.id} has been active since "
            f"{permit.issued_at.isoformat() if permit.issued_at else '?'} "
            f"with no inspection act on record ({threshold_days}+ days)"
        )
        ok = await repo.insert_risk_indicator_if_new(
            db,
            code="RI-14",
            level=RI_LEVEL_BY_CODE["RI-14"],
            object_type="permit",
            object_id=permit.id,
            description=description,
            details=details,
            idempotency_key=key,
        )
        if ok:
            written += 1
    return written


async def sweep(db: AsyncSession) -> dict[str, int]:
    """The one job `app/workers/jobs.py` calls (5-minute interval, plan ruling
    c) — harvest, then the two direct detectors."""
    return {
        "harvested": await harvest(db),
        "overlaps_raised": await sweep_overlapping_permits(db),
        "long_active_raised": await sweep_long_active_without_inspection(db),
    }


# --- The prosecutor's read surface (С22) -------------------------------------


async def list_risk_indicators(
    db: AsyncSession,
    *,
    actor: User,
    params: PageParams,
    code: str | None = None,
    level: str | None = None,
    status: str | None = None,
    object_type: str | None = None,
    object_id: uuid.UUID | None = None,
    period_from: date | None = None,
    period_to: date | None = None,
) -> tuple[list[RiskIndicator], int]:
    """Zone-scoped (an empty zone on `actor` means the whole republic, never a
    neutral default — `app/core/abac.py`) and audited on every call: С22's own
    "каждый просмотр/поиск/экспорт — в аудит" is a stricter rule than this
    codebase's general write-only audit convention, so this reader logs its
    OWN reads rather than relying on a generic HTTP-access log."""
    zone = zone_of(actor)
    items, total = await repo.list_risk_indicators(
        db,
        zone=zone,
        code=code,
        level=level,
        status=status,
        object_type=object_type,
        object_id=object_id,
        period_from=period_from,
        period_to=period_to,
        offset=params.offset,
        limit=params.page_size,
    )
    await audit.log(
        db,
        action=OVERSIGHT_VIEW_ACTION,
        user_id=actor.id,
        object_type="risk_indicator",
        basis="list",
        extra={
            "code": code,
            "level": level,
            "status": status,
            "object_type": object_type,
        },
    )
    return items, total


async def risk_indicator_counts(
    db: AsyncSession,
    *,
    actor: User,
    period_from: date | None = None,
    period_to: date | None = None,
) -> tuple[dict[str, int], dict[str, int]]:
    """`(by_code, by_level)` — `dashboard`'s risk-indicator tile calls this
    rather than re-deriving zone resolution itself (both modules are level 5,
    same-level calls are fine — design/01 rule 3): one zone-resolution
    implementation, not two that could drift. Not audited as a view of its
    own: it feeds a KPI NUMBER, never a list of identifiable rows, which is
    exactly the line С22's own "просмотр/поиск/экспорт" rule draws."""
    zone = zone_of(actor)
    rows = await repo.count_risk_indicators_by(
        db, zone=zone, period_from=period_from, period_to=period_to
    )
    by_code: dict[str, int] = {}
    by_level: dict[str, int] = {}
    for code, level, count in rows:
        by_code[code] = by_code.get(code, 0) + count
        by_level[level] = by_level.get(level, 0) + count
    return by_code, by_level


async def list_events(
    db: AsyncSession,
    *,
    actor: User,
    params: PageParams,
    event_type: str | None = None,
    object_type: str | None = None,
    period_from: date | None = None,
    period_to: date | None = None,
) -> tuple[list[OversightEvent], int]:
    """No zone predicate on `oversight_events` itself (`repo.list_events`'s
    own docstring) — `oversight.view` is what gates this route, same as
    `list_risk_indicators` above; the view is still audited."""
    items, total = await repo.list_events(
        db,
        event_type=event_type,
        object_type=object_type,
        period_from=period_from,
        period_to=period_to,
        offset=params.offset,
        limit=params.page_size,
    )
    await audit.log(
        db,
        action=OVERSIGHT_VIEW_ACTION,
        user_id=actor.id,
        object_type="oversight_event",
        basis="list",
        extra={"event_type": event_type, "object_type": object_type},
    )
    return items, total
