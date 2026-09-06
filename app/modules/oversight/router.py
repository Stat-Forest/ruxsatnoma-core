"""`GET /api/v1/oversight/*` — the prosecutor's (and central office's, and
leadership's) read-only surface (С22). Every route is gated on
`oversight.view` and reader-scoped by the caller's own zone
(`service.list_risk_indicators`); nothing here writes anything but its own
audit trail."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User
from app.modules.oversight import service
from app.modules.oversight.permissions import OVERSIGHT_VIEW
from app.modules.oversight.schemas import (
    OversightEventOut,
    RiskIndicatorCode,
    RiskIndicatorLevel,
    RiskIndicatorOut,
    RiskIndicatorStatus,
)

router = APIRouter(prefix="/oversight", tags=["oversight"])


@router.get(
    "/risk-indicators",
    response_model=Page[RiskIndicatorOut],
    dependencies=[Depends(require_permission(OVERSIGHT_VIEW))],
)
async def list_risk_indicators(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    params: Annotated[PageParams, Depends()],
    code: RiskIndicatorCode | None = None,
    level: RiskIndicatorLevel | None = None,
    status: RiskIndicatorStatus | None = None,
    object_type: str | None = None,
    object_id: uuid.UUID | None = None,
    period_from: date | None = None,
    period_to: date | None = None,
) -> Page[RiskIndicatorOut]:
    """С22's ready-made analytical slices are exactly this list filtered by
    `code` — `code=RI-07` is the SLA-violation register, `RI-01` the manual-PAID
    register, `RI-10` permits activated without payment, `RI-12` cross-zone
    access attempts, `RI-03` overlapping active permits on one contour,
    `RI-04` retroactive tariff/norm changes. The watermarked export is built
    (decision #98) as `search`'s `POST /search/exports` — `applications`/
    `permits` result sets, not this list; exporting a risk-indicator page
    itself is not something ruling #20 asked for and is not built here."""
    items, total = await service.list_risk_indicators(
        db,
        actor=user,
        params=params,
        code=code,
        level=level,
        status=status,
        object_type=object_type,
        object_id=object_id,
        period_from=period_from,
        period_to=period_to,
    )
    return Page[RiskIndicatorOut](
        items=[RiskIndicatorOut.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get(
    "/events",
    response_model=Page[OversightEventOut],
    dependencies=[Depends(require_permission(OVERSIGHT_VIEW))],
)
async def list_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    params: Annotated[PageParams, Depends()],
    event_type: str | None = None,
    object_type: str | None = None,
    period_from: date | None = None,
    period_to: date | None = None,
) -> Page[OversightEventOut]:
    """The raw accumulating stream (design/02 § oversight) — the RN payload
    once that transport exists (`tz/09`); read here as the record of every
    legally significant event this system has produced."""
    items, total = await service.list_events(
        db,
        actor=user,
        params=params,
        event_type=event_type,
        object_type=object_type,
        period_from=period_from,
        period_to=period_to,
    )
    return Page[OversightEventOut](
        items=[OversightEventOut.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )
