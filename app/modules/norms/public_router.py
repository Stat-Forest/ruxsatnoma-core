"""The anonymous surface a citizen with no session reaches from the public
`landing` site (decision #63) — no `get_current_user`, no permission code, a
rate limit instead of both. Same idiom `permits.public_router` already
established for this codebase's other anonymous surface: kept in its own file
so the whole anonymous surface of this module is one import away from a
reader asking "what can the internet reach", separate from `calc_router.py`
and `refs_router.py` where nothing else works this way.

Two narrow reference reads (`GET /public/refs/activity-types` and
`/livestock-types` — just `id`/`code`/`name`; the general, authenticated
`/refs/*` router answers far more than a public page should see) and one
anonymous, deliberately approximate price estimate (`POST
/public/calculations/estimate`). `POST /calculations/preview` keeps its own
contract untouched — this is a second, narrower door, not a loosening of that
one."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.ratelimit import rate_limit
from app.modules.norms import service
from app.modules.norms.schemas import (
    PublicActivityTypeOut,
    PublicEstimateIn,
    PublicEstimateOut,
    PublicLivestockTypeOut,
)

router = APIRouter(prefix="/public", tags=["public"])

# Both catalog reads are cheap, unfiltered SELECTs against a small, rarely
# changing table (6 activity types, 10 livestock types) — one shared bucket,
# looser than the compute endpoint's own. The estimate is the actual abuse
# target (arbitrary periods, arbitrary herd sizes, a database round trip per
# call), so it gets its own, tighter one.
_REFS_RATE_LIMIT = Depends(rate_limit("public_refs", "ratelimit_public_refs_per_minute"))
_ESTIMATE_RATE_LIMIT = Depends(
    rate_limit("public_calc_estimate", "ratelimit_public_calc_estimate_per_minute")
)


@router.get(
    "/refs/activity-types",
    response_model=list[PublicActivityTypeOut],
    dependencies=[_REFS_RATE_LIMIT],
)
async def public_activity_types(db: Annotated[AsyncSession, Depends(get_db)]) -> Any:
    return await service.list_public_activity_types(db)


@router.get(
    "/refs/livestock-types",
    response_model=list[PublicLivestockTypeOut],
    dependencies=[_REFS_RATE_LIMIT],
)
async def public_livestock_types(db: Annotated[AsyncSession, Depends(get_db)]) -> Any:
    return await service.list_public_livestock_types(db)


@router.post(
    "/calculations/estimate",
    response_model=PublicEstimateOut,
    dependencies=[_ESTIMATE_RATE_LIMIT],
)
async def public_estimate(
    payload: PublicEstimateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Any:
    return await service.estimate_public(db, payload=payload)
