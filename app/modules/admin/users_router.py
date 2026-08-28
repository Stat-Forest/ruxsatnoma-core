"""Admin API for staff user administration (С23): CRUD, credentials handout,
block/unblock/delete. Read routes accept either view or manage; every write route
requires manage. Every route is audited inside the service it calls.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.modules.admin import users_service as service
from app.modules.admin.users_schemas import (
    OneTimePasswordOut,
    PermissionCodesIn,
    PermissionCodesOut,
    PermissionOut,
    RoleAdminOut,
    RoleCreateIn,
    RolePatchIn,
    SessionAdminOut,
    SessionsRevokedOut,
    TotpUriOut,
    UserAdminOut,
    UserBlockIn,
    UserCreatedOut,
    UserCreateIn,
    UserFilters,
    UserPatchIn,
    UserStatsOut,
)
from app.modules.auth.deps import require_any_permission, require_permission
from app.modules.auth.models import User
from app.modules.auth.permissions import SESSIONS_REVOKE_ANY, USERS_MANAGE, USERS_VIEW

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=Page[UserAdminOut])
async def list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[PageParams, Depends()],
    filters: Annotated[UserFilters, Depends()],
    actor: Annotated[User, Depends(require_any_permission(USERS_VIEW, USERS_MANAGE))],
) -> Page[UserAdminOut]:
    return await service.list_users(db, params=params, filters=filters, actor=actor)


@router.get("/users/stats", response_model=UserStatsOut)
async def user_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(USERS_VIEW, USERS_MANAGE))],
) -> UserStatsOut:
    # Must stay registered before GET /users/{user_id} below — otherwise FastAPI
    # matches this path there first and tries (and fails) to parse "stats" as a
    # UUID path param.
    return await service.user_stats(db)


@router.get("/users/{user_id}", response_model=UserAdminOut)
async def get_user(
    user_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(USERS_VIEW, USERS_MANAGE))],
) -> UserAdminOut:
    return await service.get_user(db, user_id=user_id, actor=actor)


@router.post("/users", response_model=UserCreatedOut, status_code=201)
async def create_user(
    body: UserCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(USERS_MANAGE))],
) -> UserCreatedOut:
    return await service.create_user(db, data=body, actor=actor)


@router.patch("/users/{user_id}", response_model=UserAdminOut)
async def patch_user(
    user_id: uuid.UUID,
    body: UserPatchIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(USERS_MANAGE))],
) -> UserAdminOut:
    return await service.patch_user(db, user_id=user_id, data=body, actor=actor)


@router.post("/users/{user_id}/block", response_model=UserAdminOut)
async def block_user(
    user_id: uuid.UUID,
    body: UserBlockIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(USERS_MANAGE))],
) -> UserAdminOut:
    return await service.block_user(db, user_id=user_id, reason=body.reason, actor=actor)


@router.post("/users/{user_id}/unblock", response_model=UserAdminOut)
async def unblock_user(
    user_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(USERS_MANAGE))],
) -> UserAdminOut:
    return await service.unblock_user(db, user_id=user_id, actor=actor)


@router.post("/users/{user_id}/delete", response_model=UserAdminOut)
async def delete_user(
    user_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(USERS_MANAGE))],
) -> UserAdminOut:
    return await service.delete_user(db, user_id=user_id, actor=actor)


@router.post("/users/{user_id}/reset-password", response_model=OneTimePasswordOut)
async def reset_password(
    user_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(USERS_MANAGE))],
) -> OneTimePasswordOut:
    one_time_password = await service.reset_password(db, user_id=user_id, actor=actor)
    return OneTimePasswordOut(one_time_password=one_time_password)


@router.post("/users/{user_id}/reset-mfa", response_model=TotpUriOut)
async def reset_mfa(
    user_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(USERS_MANAGE))],
) -> TotpUriOut:
    totp_uri = await service.reset_mfa(db, user_id=user_id, actor=actor)
    return TotpUriOut(totp_uri=totp_uri)


@router.get("/roles", response_model=list[RoleAdminOut])
async def list_roles(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(USERS_VIEW, USERS_MANAGE))],
) -> list[RoleAdminOut]:
    return await service.list_roles(db)


@router.post("/roles", response_model=RoleAdminOut, status_code=201)
async def create_role(
    body: RoleCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(USERS_MANAGE))],
) -> RoleAdminOut:
    return await service.create_role(db, data=body, actor=actor)


@router.patch("/roles/{role_id}", response_model=RoleAdminOut)
async def patch_role(
    role_id: uuid.UUID,
    body: RolePatchIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(USERS_MANAGE))],
) -> RoleAdminOut:
    return await service.patch_role(db, role_id=role_id, data=body, actor=actor)


@router.put("/roles/{role_id}/permissions", response_model=RoleAdminOut)
async def set_role_permissions(
    role_id: uuid.UUID,
    body: PermissionCodesIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(USERS_MANAGE))],
) -> RoleAdminOut:
    return await service.set_role_permissions(db, role_id=role_id, data=body, actor=actor)


@router.post("/roles/{role_id}/archive", response_model=RoleAdminOut)
async def archive_role(
    role_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(USERS_MANAGE))],
) -> RoleAdminOut:
    return await service.archive_role(db, role_id=role_id, actor=actor)


@router.get("/permissions", response_model=list[PermissionOut])
async def list_permissions(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(USERS_VIEW, USERS_MANAGE))],
) -> list[PermissionOut]:
    return await service.list_permissions(db)


@router.get("/users/{user_id}/permissions", response_model=PermissionCodesOut)
async def get_user_permissions(
    user_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(USERS_VIEW, USERS_MANAGE))],
) -> PermissionCodesOut:
    return await service.get_user_permissions(db, user_id=user_id, actor=actor)


@router.put("/users/{user_id}/permissions", response_model=PermissionCodesOut)
async def set_user_permissions(
    user_id: uuid.UUID,
    body: PermissionCodesIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(USERS_MANAGE))],
) -> PermissionCodesOut:
    return await service.set_user_permissions(db, user_id=user_id, data=body, actor=actor)


@router.get("/users/{user_id}/sessions", response_model=list[SessionAdminOut])
async def list_user_sessions(
    user_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SESSIONS_REVOKE_ANY))],
) -> list[SessionAdminOut]:
    return await service.list_user_sessions(db, user_id=user_id, actor=actor)


@router.post("/sessions/{session_id}/revoke", status_code=204)
async def revoke_session(
    session_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SESSIONS_REVOKE_ANY))],
) -> None:
    await service.revoke_session(db, session_id=session_id, actor=actor)


@router.post("/users/{user_id}/sessions/revoke-all", response_model=SessionsRevokedOut)
async def revoke_all_sessions(
    user_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SESSIONS_REVOKE_ANY))],
) -> SessionsRevokedOut:
    revoked = await service.revoke_all_sessions(db, user_id=user_id, actor=actor)
    return SessionsRevokedOut(revoked=revoked)
