"""Contour identity and draft versions. Every route here requires
`CONTOURS_MANAGE` (the GIS specialist draws, edits and imports, but never
approves — that permission gate matches `gis_client` in the test fixtures);
Task 5 adds the approve/publish lifecycle under `CONTOURS_APPROVE` next to
these, in the same router."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User
from app.modules.gis import service
from app.modules.gis.permissions import CONTOURS_MANAGE
from app.modules.gis.schemas import (
    ContourIn,
    ContourOut,
    ContourPatch,
    VersionIn,
    VersionOut,
    VersionPatch,
)

router = APIRouter(prefix="/gis", tags=["gis"])


@router.post("/contours", status_code=201)
async def create_contour(
    payload: ContourIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
) -> ContourOut:
    contour = await service.create_contour(
        db,
        layer_id=payload.layer_id,
        organization_id=payload.organization_id,
        number=payload.number,
        kind=payload.kind,
        parent_id=payload.parent_id,
        actor=user,
    )
    return ContourOut.model_validate(contour, from_attributes=True)


@router.patch("/contours/{contour_id}")
async def patch_contour(
    contour_id: uuid.UUID,
    payload: ContourPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
) -> ContourOut:
    contour = await service.update_contour(db, contour_id, actor=user, status=payload.status)
    return ContourOut.model_validate(contour, from_attributes=True)


@router.post("/contours/{contour_id}/versions", status_code=201)
async def create_version(
    contour_id: uuid.UUID,
    payload: VersionIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
) -> VersionOut:
    version = await service.create_version(
        db,
        contour_id,
        actor=user,
        geojson=payload.geom,
        source=payload.source,
        declared_area_ha=payload.declared_area_ha,
        accuracy_m=payload.accuracy_m,
        survey_date=payload.survey_date,
        effective_from=payload.effective_from,
    )
    return VersionOut.model_validate(version, from_attributes=True)


@router.patch("/contours/{contour_id}/versions/{version_id}")
async def patch_version(
    contour_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: VersionPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
) -> VersionOut:
    version = await service.update_version(
        db, contour_id, version_id, actor=user, **payload.model_dump(exclude_unset=True)
    )
    return VersionOut.model_validate(version, from_attributes=True)
