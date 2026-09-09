"""The applicant's occupancy calendar. A NEW surface (stage 9 wave 2, ruling
#176/#177), on the SAME path prefix `gis.router` owns (`/gis/contours/{id}/
...`) but in its own module and its own file — `occupancy` neither reads nor
writes any `gis` table itself, it only names the route where the calendar
naturally lives for a client already browsing contours.

`get_current_user` and nothing else, mirroring `gis.router.get_contour_card`/
`list_contours` (ruling 5): reading a published contour needs no permission
and no zone rule, because an applicant with no role beyond "citizen" must be
able to use this while picking a plot, exactly as they already read the
contour itself."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.modules.auth.deps import get_current_user
from app.modules.auth.models import User
from app.modules.occupancy import service
from app.modules.occupancy.schemas import OccupancyOut

router = APIRouter(prefix="/gis", tags=["gis"])


@router.get("/contours/{contour_id}/occupancy", response_model=OccupancyOut)
async def get_occupancy(
    contour_id: uuid.UUID,
    activity_type_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    period_from: Annotated[date, Query(alias="from")],
    period_to: Annotated[date, Query(alias="to")],
) -> OccupancyOut:
    result = await service.get_occupancy(
        db,
        contour_id=contour_id,
        activity_type_id=activity_type_id,
        period_from=period_from,
        period_to=period_to,
    )
    return OccupancyOut.model_validate(result)
