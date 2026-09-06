"""`GET /api/v1/dashboard/*` — С21's KPI tiles and territory-slice drill-down.
Gated on `dashboard.view` (every staff role except `applicant`, `tz/03`'s own
matrix); zone-scoped by the caller's own zone, further narrowable by an
explicit region/district/organization filter (`service._filter_zone`).
A pure reader: no error code of its own, `ERR-VAL-001` for a reversed
period."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User
from app.modules.dashboard import service
from app.modules.dashboard.permissions import DASHBOARD_VIEW
from app.modules.dashboard.schemas import KpiOut, TerritorySliceOut

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get(
    "/kpi", response_model=KpiOut, dependencies=[Depends(require_permission(DASHBOARD_VIEW))]
)
async def get_kpi(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    period_from: date,
    period_to: date,
    region_id: uuid.UUID | None = None,
    district_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    activity_type_id: uuid.UUID | None = None,
    compare_previous: bool = False,
) -> KpiOut:
    result = await service.get_kpi(
        db,
        actor=user,
        region_id=region_id,
        district_id=district_id,
        organization_id=organization_id,
        activity_type_id=activity_type_id,
        period_from=period_from,
        period_to=period_to,
        compare_previous=compare_previous,
    )
    return KpiOut.model_validate(result)


@router.get(
    "/territory-slice",
    response_model=TerritorySliceOut,
    dependencies=[Depends(require_permission(DASHBOARD_VIEW))],
)
async def get_territory_slice(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    period_from: date,
    period_to: date,
    region_id: uuid.UUID | None = None,
    district_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
) -> TerritorySliceOut:
    """One drill-down level per call: no filter -> regions; `region_id` ->
    that region's districts; `district_id` -> that district's organizations;
    `organization_id` -> that organization's contours (the level k-anonymity
    (ruling d) almost always bites at). Pass the id the PREVIOUS response's
    cell named to go one level deeper."""
    result = await service.get_territory_slice(
        db,
        actor=user,
        region_id=region_id,
        district_id=district_id,
        organization_id=organization_id,
        period_from=period_from,
        period_to=period_to,
    )
    return TerritorySliceOut.model_validate(result)
