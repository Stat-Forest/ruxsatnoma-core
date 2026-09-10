"""Ruling #177 (stage 9): the leshoz x activity season/minimum-term
dictionary — a leshoz states its season once instead of on every one of its
contours, and a contour missing it entirely is no longer silent
(`checks._season_check`/`_min_term_check`).

Write routes carry `ACTIVITY_SEASONS_MANAGE` and are additionally zone-checked
inside `service` (`_assert_organization_zone`), the same split every norms
write route uses (a per-endpoint guard is not a root fix when several
actions share the same precondition — lesson): a zone-scoped actor (the
leshoz's own gis_specialist) may act only on their own organization; a
zone-free actor (central office) may act on any. Read routes are open to any
authenticated user, the same reasoning `norms.router`'s own module docstring
gives for `Norm`: a front-end has to explain a refusal."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.schemas import PAGING_MAX, Page
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User
from app.modules.norms import service
from app.modules.norms.permissions import ACTIVITY_SEASONS_MANAGE
from app.modules.norms.schemas import (
    ActivitySeasonIn,
    ActivitySeasonOut,
    ActivitySeasonPatch,
    EffectiveSeasonOut,
)

router = APIRouter(tags=["norms"])


@router.get("/activity-seasons", response_model=Page[ActivitySeasonOut])
async def list_activity_seasons(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
    organization_id: uuid.UUID | None = None,
    activity_type_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=PAGING_MAX)] = 0,
) -> Any:
    items, total = await service.list_activity_seasons(
        db,
        organization_id=organization_id,
        activity_type_id=activity_type_id,
        limit=limit,
        offset=offset,
    )
    return Page[ActivitySeasonOut](
        items=[ActivitySeasonOut.model_validate(item) for item in items],
        total=total,
        page=offset // limit + 1,
        page_size=limit,
    )


# Declared BEFORE `/activity-seasons/{activity_season_id}` — FastAPI matches
# routes in declaration order, and "effective" would otherwise be swallowed
# by the UUID path parameter below.
@router.get("/activity-seasons/effective", response_model=EffectiveSeasonOut)
async def effective_season(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
    activity_type_id: uuid.UUID,
    contour_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
) -> Any:
    return await service.effective_season(
        db,
        activity_type_id=activity_type_id,
        contour_id=contour_id,
        organization_id=organization_id,
    )


@router.get("/activity-seasons/{activity_season_id}", response_model=ActivitySeasonOut)
async def get_activity_season(
    activity_season_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
) -> Any:
    return await service.get_activity_season(db, activity_season_id)


@router.post("/activity-seasons", response_model=ActivitySeasonOut, status_code=201)
async def create_activity_season(
    payload: ActivitySeasonIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ACTIVITY_SEASONS_MANAGE))],
) -> Any:
    return await service.create_activity_season(db, payload, actor=actor)


@router.patch("/activity-seasons/{activity_season_id}", response_model=ActivitySeasonOut)
async def update_activity_season(
    activity_season_id: uuid.UUID,
    payload: ActivitySeasonPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ACTIVITY_SEASONS_MANAGE))],
) -> Any:
    return await service.update_activity_season(db, activity_season_id, payload, actor=actor)
