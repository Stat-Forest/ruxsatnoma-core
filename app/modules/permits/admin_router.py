"""Ratings the Agency and each leshoz read (`ratings.view`, ruling #142). The
citizen's write side is `router.py`; this split mirrors `help.admin_router`.

Neither route below carries the applicant, the permit id or the permit number
in its response — ruling #141, closed by construction in `schemas.
RatingCommentRow` and asserted on the SERIALIZED body by
`test_comments_never_name_the_author` rather than on the schema alone.
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User
from app.modules.permits import service
from app.modules.permits.permissions import RATINGS_VIEW
from app.modules.permits.schemas import RatingCommentRow, RatingsSummaryOut

router = APIRouter(prefix="/admin/ratings", tags=["admin"])
_VIEW = Depends(require_permission(RATINGS_VIEW))


@router.get("/summary")
async def get_ratings_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, _VIEW],
    period_from: date,
    period_to: date,
    organization_id: uuid.UUID | None = None,
    activity_type_id: uuid.UUID | None = None,
) -> RatingsSummaryOut:
    """The overall average and count over the caller's own zone
    (`app.core.abac.zone_filter`, all three axes: region, district AND
    organization — never `organization_id` alone, the finding
    `dashboard.repo.permits_kpi` already closed), plus the same pair broken
    down by organization and by activity type. `organization_id`/
    `activity_type_id` narrow the zone further; neither widens it.
    """
    result = await service.ratings_summary(
        db,
        actor=actor,
        organization_id=organization_id,
        activity_type_id=activity_type_id,
        period_from=period_from,
        period_to=period_to,
    )
    return RatingsSummaryOut.model_validate(result)


@router.get("")
async def list_ratings(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, _VIEW],
    params: Annotated[PageParams, Depends()],
    period_from: date,
    period_to: date,
) -> Page[RatingCommentRow]:
    """The anonymous comment feed: date, service, leshoz, score, text — never
    who left it. Zone-scoped the same way the summary above is."""
    items, total = await service.list_rating_comments(
        db, actor=actor, params=params, period_from=period_from, period_to=period_to
    )
    return Page[RatingCommentRow](
        items=[RatingCommentRow.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )
