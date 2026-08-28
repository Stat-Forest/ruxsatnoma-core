"""Announcements API: `router` is the audience-filtered reader open to every
authenticated user; `admin_router` is the CRUD surface gated behind
`admin.announcements.manage`. Both are mounted at `/api/v1` by app/main.py."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.modules.admin import announcements_service as service
from app.modules.admin.announcements_service import (
    AnnouncementAdminOut,
    AnnouncementCreateIn,
    AnnouncementOut,
    AnnouncementPatchIn,
)
from app.modules.admin.permissions import ANNOUNCEMENTS_MANAGE
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User

router = APIRouter(prefix="/announcements", tags=["announcements"])
admin_router = APIRouter(prefix="/admin/announcements", tags=["admin"])


@router.get("", response_model=Page[AnnouncementOut])
async def list_announcements(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[PageParams, Depends()],
    user: Annotated[User, Depends(get_current_user)],
) -> Page[AnnouncementOut]:
    return await service.list_public(db, params=params, user=user)


@router.get("/{announcement_id}", response_model=AnnouncementOut)
async def get_announcement(
    announcement_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> AnnouncementOut:
    return await service.get_public(db, announcement_id=announcement_id, user=user)


@admin_router.get("", response_model=Page[AnnouncementAdminOut])
async def admin_list_announcements(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[PageParams, Depends()],
    actor: Annotated[User, Depends(require_permission(ANNOUNCEMENTS_MANAGE))],
    status: str | None = None,
) -> Page[AnnouncementAdminOut]:
    return await service.list_admin(db, params=params, status=status)


@admin_router.get("/{announcement_id}", response_model=AnnouncementAdminOut)
async def admin_get_announcement(
    announcement_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ANNOUNCEMENTS_MANAGE))],
) -> AnnouncementAdminOut:
    return await service.get_admin(db, announcement_id=announcement_id)


@admin_router.post("", response_model=AnnouncementAdminOut, status_code=201)
async def create_announcement(
    body: AnnouncementCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ANNOUNCEMENTS_MANAGE))],
) -> AnnouncementAdminOut:
    return await service.create(db, data=body, actor=actor)


@admin_router.patch("/{announcement_id}", response_model=AnnouncementAdminOut)
async def patch_announcement(
    announcement_id: uuid.UUID,
    body: AnnouncementPatchIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ANNOUNCEMENTS_MANAGE))],
) -> AnnouncementAdminOut:
    return await service.patch(db, announcement_id=announcement_id, data=body, actor=actor)


@admin_router.post("/{announcement_id}/publish", response_model=AnnouncementAdminOut)
async def publish_announcement(
    announcement_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ANNOUNCEMENTS_MANAGE))],
) -> AnnouncementAdminOut:
    return await service.publish(db, announcement_id=announcement_id, actor=actor)


@admin_router.post("/{announcement_id}/archive", response_model=AnnouncementAdminOut)
async def archive_announcement(
    announcement_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ANNOUNCEMENTS_MANAGE))],
) -> AnnouncementAdminOut:
    return await service.archive(db, announcement_id=announcement_id, actor=actor)
