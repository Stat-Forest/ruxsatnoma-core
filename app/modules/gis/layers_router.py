"""The layer catalogue, and the restriction/protection/fire-ban objects that
live inside its layers (task 6). Reading the catalogue is open to any
authenticated user — an applicant must be able to see which layers exist to
pick a plot (ruling 18); writing a layer OBJECT (a fire ban, a water point)
requires `gis.layers.manage`, the same permission `PATCH /layers/{code}`
already uses for the layer's own presentation."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User
from app.modules.gis import service
from app.modules.gis.permissions import LAYERS_MANAGE
from app.modules.gis.schemas import (
    FeatureCollectionOut,
    FeatureIn,
    FeatureOut,
    FeaturePatch,
    LayerList,
    LayerOut,
    LayerPatch,
)

router = APIRouter(prefix="/gis", tags=["gis"])


@router.get("/layers", response_model=LayerList)
async def list_layers(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> LayerList:
    layers = await service.list_layers(db)
    return LayerList(items=[LayerOut.model_validate(x, from_attributes=True) for x in layers])


@router.patch("/layers/{code}", response_model=LayerOut)
async def patch_layer(
    code: str,
    payload: LayerPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(LAYERS_MANAGE))],
) -> LayerOut:
    layer = await service.update_layer(
        db,
        code,
        actor=user,
        style=payload.style,
        is_public=payload.is_public,
        status=payload.status,
    )
    return LayerOut.model_validate(layer, from_attributes=True)


@router.get("/layers/{code}/features")
async def list_features(
    code: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    bbox: str | None = None,
    valid_on: date | None = None,
) -> FeatureCollectionOut:
    """No permission required (ruling 5, mirrors `GET /gis/layers` — ruling
    18): the service itself refuses a non-public layer to an applicant
    specifically (`ERR-ACL-001`), a ROLE gate rather than a held-grant one."""
    collection = await service.list_features(db, code, bbox=bbox, valid_on=valid_on, actor=user)
    return FeatureCollectionOut.model_validate(collection)


@router.post("/layers/{code}/features", status_code=201)
async def create_feature(
    code: str,
    payload: FeatureIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(LAYERS_MANAGE))],
) -> FeatureOut:
    feature = await service.create_feature(
        db,
        code,
        geojson=payload.geom,
        organization_id=payload.organization_id,
        name=payload.name.root if payload.name is not None else None,
        props=payload.props,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
        actor=user,
    )
    return FeatureOut.model_validate(feature, from_attributes=True)


@router.patch("/layers/{code}/features/{feature_id}")
async def patch_feature(
    code: str,
    feature_id: uuid.UUID,
    payload: FeaturePatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(LAYERS_MANAGE))],
) -> FeatureOut:
    feature = await service.update_feature(
        db, feature_id, actor=user, **payload.model_dump(exclude_unset=True)
    )
    return FeatureOut.model_validate(feature, from_attributes=True)


@router.post("/layers/{code}/features/{feature_id}/publish")
async def publish_feature(
    code: str,
    feature_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(LAYERS_MANAGE))],
) -> FeatureOut:
    feature = await service.publish_feature(db, feature_id, actor=user)
    return FeatureOut.model_validate(feature, from_attributes=True)


@router.post("/layers/{code}/features/{feature_id}/archive")
async def archive_feature(
    code: str,
    feature_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(LAYERS_MANAGE))],
) -> FeatureOut:
    feature = await service.archive_feature(db, feature_id, actor=user)
    return FeatureOut.model_validate(feature, from_attributes=True)
