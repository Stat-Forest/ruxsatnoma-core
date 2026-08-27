"""Auth HTTP routes (design/03 § auth)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.modules.auth import repo, service
from app.modules.auth.deps import get_current_session, get_current_user
from app.modules.auth.models import Session, User
from app.modules.auth.schemas import MeOut, RoleOut, UserOut, ZoneOut

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
