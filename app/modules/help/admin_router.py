"""FAQ CRUD — `help.faq.manage` on every route (`router.py`'s public `GET
/help/faq` is the read side, mirroring `admin.announcements_router`'s own
`router`/`admin_router` split)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User
from app.modules.help import service
from app.modules.help.permissions import FAQ_MANAGE
from app.modules.help.schemas import FaqIn, FaqOut, FaqPatch

router = APIRouter(prefix="/admin/help/faq", tags=["admin"])
_MANAGE = Depends(require_permission(FAQ_MANAGE))


@router.get("", response_model=list[FaqOut], dependencies=[_MANAGE])
async def list_faq(db: Annotated[AsyncSession, Depends(get_db)], status: str | None = None) -> Any:
    return await service.list_faq_admin(db, status=status)


@router.post("", response_model=FaqOut, status_code=201)
async def create_faq(
    payload: FaqIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(FAQ_MANAGE))],
) -> Any:
    return await service.create_faq(db, data=payload, actor=user)


@router.patch("/{faq_id}", response_model=FaqOut)
async def update_faq(
    faq_id: uuid.UUID,
    payload: FaqPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(FAQ_MANAGE))],
) -> Any:
    return await service.update_faq(db, faq_id, data=payload, actor=user)
