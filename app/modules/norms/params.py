"""Loads everything a calculation needs, once, into a frozen snapshot. The
calculator never queries; this module never computes."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin import repo as admin_repo
from app.modules.norms import calculator, repo, service
from app.modules.norms.calculator import CalcRequest, NormFact, ParamSnapshot, TariffFact

BASE_CODES = (
    "bhm",
    "safety_reserve",
    "sb_feed_norm",
    "season_share",
    "rounding_money",
    "rounding_heads",
)


async def load_limit_params(db: AsyncSession, *, on_date: date) -> dict[str, Any]:
    """The three numbers `max_sb` needs, plus rounding. Used by `publish_norm`,
    which has no request to snapshot."""
    rows = await repo.effective_parameters(db, BASE_CODES, on_date)
    return {code: row.value for code, row in rows.items()}


async def load_snapshot(
    db: AsyncSession,
    *,
    request: CalcRequest,
    contour_id: uuid.UUID | None,
    activity_type_id: uuid.UUID,
) -> ParamSnapshot:
    """Base parameters + the `coef_sb:`/`tariff_group:`/`tariff_exempt:`
    families + the tariffs in force + the norm in force + the committed load
    from LOAD_PROVIDERS.

    `contour_id` is optional: a calculation with no contour chosen yet (or for
    an activity with no per-contour norm at all) simply gets `norm=None` and
    `load_sb=Decimal("0")`/`load_source="none"` — the same shape a contour with
    nothing committed against it would produce, since `service.committed_load_sb`
    itself answers "none" whenever `LOAD_PROVIDERS` is empty (ruling 12)."""
    values: dict[str, Any] = await load_limit_params(db, on_date=request.on_date)
    coef_rows = await repo.effective_parameters_by_prefix(db, "coef_sb:", request.on_date)
    group_rows = await repo.effective_parameters_by_prefix(db, "tariff_group:", request.on_date)
    # C2: which activities the law genuinely leaves un-tariffed, as a dated
    # row rather than a constant in `calculator.py` — see `_is_tariff_exempt`.
    exempt_rows = await repo.effective_parameters_by_prefix(db, "tariff_exempt:", request.on_date)
    values.update({code: row.value for code, row in coef_rows.items()})
    values.update({code: row.value for code, row in group_rows.items()})
    values.update({code: row.value for code, row in exempt_rows.items()})

    # The unit belongs to the ACTIVITY, not to its tariff: `science` has no
    # tariff row by law and still measures something. Read through `admin.repo`
    # like every other reference-data lookup here, never a direct
    # `activity_types` query (module boundary, 'Reference data').
    activity_types = await admin_repo.list_activity_types(db)
    quantity_unit = next(
        (a.quantity_unit for a in activity_types if a.id == activity_type_id), None
    )

    tariff_rows = await repo.effective_tariffs(db, activity_type_id, request.on_date)
    tariffs = tuple(
        TariffFact(
            id=row.id,
            livestock_group=row.livestock_group,
            coefficient=row.coefficient,
            quantity_unit=row.quantity_unit,
            benefit_modifiers=row.benefit_modifiers,
        )
        for row in tariff_rows
    )

    norm: NormFact | None = None
    load_sb, load_source = Decimal("0"), "none"
    capacity_load, capacity_load_source = Decimal("0"), "none"
    occupied_until: date | None = None
    occupied_until_source = "none"
    if contour_id is not None:
        norm_row = await repo.effective_norm(db, contour_id, activity_type_id, request.on_date)
        if norm_row is not None:
            norm = NormFact(
                id=norm_row.id,
                yield_c_per_ha=norm_row.yield_c_per_ha,
                max_sb=norm_row.max_sb,
                season=norm_row.season,
                rotation=norm_row.rotation,
                capacity=norm_row.capacity,
            )
        load_sb, load_source = await service.committed_load_sb(
            db, contour_id, request.period_from, request.period_to
        )
        # Ruling #176: which of the two NEW seams this request needs depends
        # on whether a capacity resolves at all — never both, and never the
        # wrong one. A capacity contour needs its committed quantity (this
        # activity's own unit, `CAPACITY_LOAD_PROVIDERS`); a capacity-less one
        # needs `EXCLUSIVITY_PROVIDERS` instead, since a sum can never answer
        # "which day does it free up". Grazing resolves through `max_sb`
        # above and its own `LOAD_PROVIDERS`-backed `load_sb`, so neither call
        # below ever fires for it.
        capacity = calculator.resolve_capacity(request.activity_code, norm)
        if capacity is None:
            occupied_until, occupied_until_source = await service.occupied_until(
                db, contour_id, activity_type_id, request.period_from, request.period_to
            )
        elif request.activity_code != calculator.GRAZING:
            capacity_load, capacity_load_source = await service.committed_capacity_load(
                db, contour_id, activity_type_id, request.period_from, request.period_to
            )

    return ParamSnapshot(
        values=values,
        tariffs=tariffs,
        norm=norm,
        load_sb=load_sb,
        load_source=load_source,
        capacity_load=capacity_load,
        capacity_load_source=capacity_load_source,
        occupied_until=occupied_until,
        occupied_until_source=occupied_until_source,
        quantity_unit=quantity_unit,
    )
