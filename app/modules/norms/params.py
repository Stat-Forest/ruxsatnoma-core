"""Loads everything a calculation needs, once, into a frozen snapshot. The
calculator never queries; this module never computes."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.norms import repo, service
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
    """Base parameters + the `coef_sb:`/`tariff_group:` families + the tariffs in
    force + the norm in force + the committed load from LOAD_PROVIDERS.

    `contour_id` is optional: a calculation with no contour chosen yet (or for
    an activity with no per-contour norm at all) simply gets `norm=None` and
    `load_sb=Decimal("0")`/`load_source="none"` — the same shape a contour with
    nothing committed against it would produce, since `service.committed_load_sb`
    itself answers "none" whenever `LOAD_PROVIDERS` is empty (ruling 12)."""
    values: dict[str, Any] = await load_limit_params(db, on_date=request.on_date)
    coef_rows = await repo.effective_parameters_by_prefix(db, "coef_sb:", request.on_date)
    group_rows = await repo.effective_parameters_by_prefix(db, "tariff_group:", request.on_date)
    values.update({code: row.value for code, row in coef_rows.items()})
    values.update({code: row.value for code, row in group_rows.items()})

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
    if contour_id is not None:
        norm_row = await repo.effective_norm(db, contour_id, activity_type_id, request.on_date)
        if norm_row is not None:
            norm = NormFact(
                id=norm_row.id,
                yield_c_per_ha=norm_row.yield_c_per_ha,
                max_sb=norm_row.max_sb,
                season=norm_row.season,
                rotation=norm_row.rotation,
            )
        load_sb, load_source = await service.committed_load_sb(
            db, contour_id, request.period_from, request.period_to
        )

    return ParamSnapshot(
        values=values, tariffs=tariffs, norm=norm, load_sb=load_sb, load_source=load_source
    )
