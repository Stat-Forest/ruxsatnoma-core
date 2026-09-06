"""Dashboard service — the module's only door for everyone else. С21's KPI
tiles and the republic -> region -> district -> organization -> contour
drill-down, with k-anonymity suppression at every level (plan ruling d).

Every tile below has a real, named source (`repo.py`'s own docstrings); two
С21 names this stage — inspections and violations — could not be built for
lack of one (`inspections`, a sibling 4.1 track, is not merged into `dev` yet)
and are reported as `omitted`, never filled with a plausible constant (the
track brief's own rule, already applied once when the applicant's dashboard
was built)."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.abac import Zone, zone_of
from app.core.errors import err
from app.modules.auth.models import User
from app.modules.dashboard import repo
from app.modules.gis import service as gis_service
from app.modules.oversight import service as oversight_service

OMITTED_TILES = (
    "inspections_count: the 4.1 `inspections` module is not merged into `dev` yet",
    "violations_count: same reason (inspections.violation_cases does not exist)",
)


def _filter_zone(
    region_id: uuid.UUID | None, district_id: uuid.UUID | None, organization_id: uuid.UUID | None
) -> Zone:
    return Zone(region_id, district_id, organization_id)


def _validate_period(period_from: date, period_to: date) -> None:
    """Fail-closed at the shared entry point, before anything runs (the
    lesson: a reversed period silently inverts a range predicate and hides
    the rows it should find) — mirrors `norms.checks`'s own guard."""
    if period_to < period_from:
        raise err("ERR-VAL-001", details={"reason": "period_reversed"})


async def get_kpi(
    db: AsyncSession,
    *,
    actor: User,
    region_id: uuid.UUID | None,
    district_id: uuid.UUID | None,
    organization_id: uuid.UUID | None,
    activity_type_id: uuid.UUID | None,
    period_from: date,
    period_to: date,
    compare_previous: bool,
) -> dict:
    _validate_period(period_from, period_to)
    actor_zone = zone_of(actor)
    filter_zone = _filter_zone(region_id, district_id, organization_id)

    issued_count, active_count = await repo.permits_kpi(
        db,
        actor_zone=actor_zone,
        filter_zone=filter_zone,
        activity_type_id=activity_type_id,
        period_from=period_from,
        period_to=period_to,
    )
    previous_issued: int | None = None
    previous_total: int | None = None
    if compare_previous:
        span = (period_to - period_from) + timedelta(days=1)
        prev_from = period_from - span
        prev_to = period_from - timedelta(days=1)
        previous_issued, _ = await repo.permits_kpi(
            db,
            actor_zone=actor_zone,
            filter_zone=filter_zone,
            activity_type_id=activity_type_id,
            period_from=prev_from,
            period_to=prev_to,
        )
        previous_by_status = await repo.applications_by_status(
            db,
            actor_zone=actor_zone,
            filter_zone=filter_zone,
            activity_type_id=activity_type_id,
            period_from=prev_from,
            period_to=prev_to,
        )
        previous_total = sum(previous_by_status.values())

    by_status = await repo.applications_by_status(
        db,
        actor_zone=actor_zone,
        filter_zone=filter_zone,
        activity_type_id=activity_type_id,
        period_from=period_from,
        period_to=period_to,
    )
    sb_load = await repo.sb_load_total(
        db, actor_zone=actor_zone, filter_zone=filter_zone, activity_type_id=activity_type_id
    )
    contours = await repo.published_contours_in_scope(
        db, actor_zone=actor_zone, filter_zone=filter_zone
    )
    contour_ids = [contour_id for contour_id, _ in contours]
    avg_occupied_pct: Decimal | None = None
    if contour_ids:
        occupied_by_id, _source = await gis_service.occupancy_map(db, contour_ids)
        total_area = sum((area for _, area in contours), Decimal(0))
        total_occupied = sum(
            (occupied_by_id.get(cid, Decimal(0)) for cid in contour_ids), Decimal(0)
        )
        if total_area > 0:
            avg_occupied_pct = (total_occupied / total_area * 100).quantize(Decimal("0.01"))

    active_sla, overdue_sla = await repo.sla_kpi(
        db,
        actor_zone=actor_zone,
        filter_zone=filter_zone,
        activity_type_id=activity_type_id,
        now=datetime.now(UTC),
    )
    reasons = await repo.rejection_reasons(
        db,
        actor_zone=actor_zone,
        filter_zone=filter_zone,
        activity_type_id=activity_type_id,
        period_from=period_from,
        period_to=period_to,
    )
    invoiced, paid, budget_share, recipient_share = await repo.payments_kpi(
        db,
        actor_zone=actor_zone,
        filter_zone=filter_zone,
        period_from=period_from,
        period_to=period_to,
    )
    by_code, by_level = await oversight_service.risk_indicator_counts(
        db, actor=actor, period_from=period_from, period_to=period_to
    )

    return {
        "period": {"period_from": period_from, "period_to": period_to},
        "permits": {
            "issued_count": issued_count,
            "active_count": active_count,
            "previous_issued_count": previous_issued,
        },
        "applications": {
            "total_count": sum(by_status.values()),
            "by_status": by_status,
            "previous_total_count": previous_total,
        },
        "occupancy": {"contour_count": len(contour_ids), "avg_occupied_pct": avg_occupied_pct},
        "sb_load_total": sb_load,
        "payments": {
            "invoiced_amount": invoiced,
            "paid_amount": paid,
            "budget_share_amount": budget_share,
            "recipient_share_amount": recipient_share,
        },
        "sla": {"active_count": active_sla, "overdue_count": overdue_sla},
        "rejections": [{"reason_item_id": rid, "count": count} for rid, count in reasons],
        "risk_indicators": {"by_code": by_code, "by_level": by_level},
        "omitted": list(OMITTED_TILES),
    }


async def get_territory_slice(
    db: AsyncSession,
    *,
    actor: User,
    region_id: uuid.UUID | None,
    district_id: uuid.UUID | None,
    organization_id: uuid.UUID | None,
    period_from: date,
    period_to: date,
) -> dict:
    """The drill-down С21 asks for: republic (no filter) -> region -> district
    -> organization -> contour, one level per call, each narrowed by the
    PARENT id already chosen. K-anonymity (ruling d) is applied uniformly at
    EVERY level for EVERY viewer, including leadership — a dashboard slice is
    a trends tool, not the case-lookup screen."""
    _validate_period(period_from, period_to)
    actor_zone = zone_of(actor)
    threshold = await settings_store.get_int(db, "dashboard_k_anonymity_threshold")

    if organization_id is not None:
        level = "contour"
        raw = await repo.slice_by_contour(
            db,
            actor_zone=actor_zone,
            organization_id=organization_id,
            period_from=period_from,
            period_to=period_to,
        )
    elif district_id is not None:
        level = "organization"
        raw = await repo.slice_by_organization(
            db,
            actor_zone=actor_zone,
            district_id=district_id,
            period_from=period_from,
            period_to=period_to,
        )
    elif region_id is not None:
        level = "district"
        raw = await repo.slice_by_district(
            db,
            actor_zone=actor_zone,
            region_id=region_id,
            period_from=period_from,
            period_to=period_to,
        )
    else:
        level = "region"
        raw = await repo.slice_by_region(
            db, actor_zone=actor_zone, period_from=period_from, period_to=period_to
        )

    cells = []
    for row in raw:
        suppressed = row["applicant_count"] < threshold
        cells.append(
            {
                "level": level,
                "key": row["key"],
                "label": row["label"],
                "applications_count": None if suppressed else row["applications_count"],
                "permits_count": None if suppressed else row["permits_count"],
                "applicant_count": None if suppressed else row["applicant_count"],
                "suppressed": suppressed,
            }
        )
    return {"level": level, "k_anonymity_threshold": threshold, "cells": cells}
