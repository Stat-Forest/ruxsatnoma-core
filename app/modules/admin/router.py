"""Admin write API for reference data (С23). Every route is permission-gated and
audited; nothing here deletes rows."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.modules.admin import service
from app.modules.admin.permissions import CLASSIFIERS_MANAGE, ORGANIZATIONS_MANAGE, SETTINGS_MANAGE
from app.modules.admin.schemas import (
    ClassifierIn,
    ClassifierItemIn,
    ClassifierItemOut,
    ClassifierItemPatch,
    OrganizationAdminOut,
    OrganizationIn,
    OrganizationPatch,
    SettingIn,
    SettingOut,
)
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


@router.get("/organizations/{org_id}", response_model=OrganizationAdminOut)
async def get_organization(
    org_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ORGANIZATIONS_MANAGE))],
) -> OrganizationAdminOut:
    """The only way to read `requisites` back outside a write response — `/refs`
    deliberately omits it (finding 6, whole-branch review)."""
    org = await service.organization_or_404(db, org_id)
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


@router.post("/classifiers", status_code=201)
async def create_classifier(
    body: ClassifierIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(CLASSIFIERS_MANAGE))],
) -> dict[str, Any]:
    classifier = await service.create_classifier(db, data=body, actor=actor)
    return {"id": str(classifier.id), "code": classifier.code, "name": classifier.name}


@router.post("/classifiers/{code}/items", response_model=ClassifierItemOut, status_code=201)
async def add_classifier_item(
    code: str,
    body: ClassifierItemIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(CLASSIFIERS_MANAGE))],
) -> ClassifierItemOut:
    item = await service.add_classifier_item(db, classifier_code=code, data=body, actor=actor)
    return ClassifierItemOut.model_validate(item, from_attributes=True)


@router.patch("/classifier-items/{item_id}", response_model=ClassifierItemOut)
async def update_classifier_item(
    item_id: uuid.UUID,
    body: ClassifierItemPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(CLASSIFIERS_MANAGE))],
) -> ClassifierItemOut:
    item = await service.update_classifier_item(db, item_id=item_id, patch=body, actor=actor)
    return ClassifierItemOut.model_validate(item, from_attributes=True)


@router.post("/classifier-items/{item_id}/archive", response_model=ClassifierItemOut)
async def archive_classifier_item(
    item_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(CLASSIFIERS_MANAGE))],
) -> ClassifierItemOut:
    item = await service.archive_classifier_item(db, item_id=item_id, actor=actor)
    return ClassifierItemOut.model_validate(item, from_attributes=True)


@router.post(
    "/classifier-items/{item_id}/supersede", response_model=ClassifierItemOut, status_code=201
)
async def supersede_classifier_item(
    item_id: uuid.UUID,
    body: ClassifierItemIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(CLASSIFIERS_MANAGE))],
) -> ClassifierItemOut:
    item = await service.supersede_classifier_item(db, item_id=item_id, data=body, actor=actor)
    return ClassifierItemOut.model_validate(item, from_attributes=True)


@router.get("/settings", response_model=list[SettingOut])
async def list_settings(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SETTINGS_MANAGE))],
) -> list[SettingOut]:
    return await service.list_settings(db)


@router.put("/settings/{key}", response_model=SettingOut)
async def update_setting(
    key: str,
    body: SettingIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SETTINGS_MANAGE))],
) -> SettingOut:
    return await service.update_setting(db, key=key, raw_value=body.value, actor=actor)
