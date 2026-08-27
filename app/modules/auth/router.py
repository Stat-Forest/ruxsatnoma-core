"""Auth HTTP routes (design/03 § auth)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.deps import get_db
from app.modules.auth import repo, service
from app.modules.auth.deps import get_current_session, get_current_user
from app.modules.auth.models import Session, User
from app.modules.auth.schemas import (
    LoginIn,
    LoginOut,
    MeOut,
    MfaIn,
    PasswordChangeIn,
    RoleOut,
    UserOut,
    ZoneOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=MeOut)
async def me(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MeOut:
    role = await repo.get_role(db, user.role_id)
    assert role is not None  # FK guarantees it
    return MeOut(
        user=UserOut.model_validate(user, from_attributes=True),
        role=RoleOut.model_validate(role, from_attributes=True),
        permissions=sorted(await repo.permission_codes(db, user)),
        zone=ZoneOut(
            region_id=user.region_id,
            district_id=user.district_id,
            organization_id=user.organization_id,
        ),
    )


@router.post("/logout", status_code=204)
async def logout(
    session_row: Annotated[Session, Depends(get_current_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
    response: Response,
) -> None:
    await service.revoke_session(db, session_row, reason="logout")
    response.delete_cookie("session")
    response.delete_cookie("csrf_token")


@router.post("/login", response_model=LoginOut)
async def login(
    body: LoginIn, request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> LoginOut:
    token = await service.login_password(
        db,
        login=body.login,
        password=body.password,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
    )
    return LoginOut(mfa_required=True, mfa_token=token)


@router.post("/mfa/verify", response_model=MeOut)
async def mfa_verify(
    body: MfaIn,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MeOut:
    user, _row, token, csrf = await service.verify_mfa(
        db,
        mfa_token=body.mfa_token,
        code=body.code,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
    )
    secure = get_settings().resolve_cookie_secure()
    response.set_cookie("session", token, httponly=True, samesite="lax", secure=secure, path="/")
    response.set_cookie("csrf_token", csrf, httponly=False, samesite="lax", secure=secure, path="/")
    role = await repo.get_role(db, user.role_id)
    assert role is not None
    return MeOut(
        user=UserOut.model_validate(user, from_attributes=True),
        role=RoleOut.model_validate(role, from_attributes=True),
        permissions=sorted(await repo.permission_codes(db, user)),
        zone=ZoneOut(
            region_id=user.region_id,
            district_id=user.district_id,
            organization_id=user.organization_id,
        ),
    )


@router.post("/password/change", status_code=204)
async def password_change(
    body: PasswordChangeIn,
    user: Annotated[User, Depends(get_current_user)],
    session_row: Annotated[Session, Depends(get_current_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await service.change_password(
        db, user, old=body.old_password, new=body.new_password, current_session_id=session_row.id
    )
