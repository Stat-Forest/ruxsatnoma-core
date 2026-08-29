"""The layer catalogue. Reading is open to any authenticated user — an applicant
must be able to see which layers exist to pick a plot (ruling 18)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User
from app.modules.gis import repo, service
from app.modules.gis.permissions import LAYERS_MANAGE
from app.modules.gis.schemas import LayerList, LayerOut, LayerPatch

router = APIRouter(prefix="/gis", tags=["gis"])


@router.get("/layers", response_model=LayerList)
async def list_layers(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> LayerList:
    layers = await repo.list_layers(db)
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
