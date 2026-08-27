"""Admin write API for reference data (С23). Every route is permission-gated and
audited; nothing here deletes rows."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.modules.admin import service
from app.modules.admin.permissions import ORGANIZATIONS_MANAGE
from app.modules.admin.schemas import OrganizationAdminOut, OrganizationIn, OrganizationPatch
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/organizations", response_model=OrganizationAdminOut, status_code=201)
async def create_organization(
    body: OrganizationIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ORGANIZATIONS_MANAGE))],
) -> OrganizationAdminOut:
    org = await service.create_organization(db, data=body, actor=actor)
    return OrganizationAdminOut.model_validate(org, from_attributes=True)


@router.patch("/organizations/{org_id}", response_model=OrganizationAdminOut)
async def update_organization(
    org_id: uuid.UUID,
    body: OrganizationPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ORGANIZATIONS_MANAGE))],
) -> OrganizationAdminOut:
    org = await service.update_organization(db, org_id=org_id, patch=body, actor=actor)
    return OrganizationAdminOut.model_validate(org, from_attributes=True)


@router.post("/organizations/{org_id}/archive", response_model=OrganizationAdminOut)
async def archive_organization(
    org_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ORGANIZATIONS_MANAGE))],
) -> OrganizationAdminOut:
    org = await service.archive_organization(db, org_id=org_id, actor=actor)
    return OrganizationAdminOut.model_validate(org, from_attributes=True)
