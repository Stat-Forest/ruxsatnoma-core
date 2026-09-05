"""Staff triage of citizen appeals — `public.appeals.manage` on every route."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User
from app.modules.public import service
from app.modules.public.permissions import APPEALS_MANAGE
from app.modules.public.schemas import AppealAdminOut, AppealAnswerIn, AppealStatusIn

router = APIRouter(prefix="/admin/public", tags=["public"])
_MANAGE = Depends(require_permission(APPEALS_MANAGE))


@router.get("/appeals", response_model=Page[AppealAdminOut], dependencies=[_MANAGE])
async def list_appeals(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[PageParams, Depends()],
    status: str | None = None,
) -> Any:
    items, total = await service.list_appeals(db, status=status, params=params)
    return Page[AppealAdminOut](
        items=[AppealAdminOut.model_validate(x, from_attributes=True) for x in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/appeals/{appeal_id}", response_model=AppealAdminOut, dependencies=[_MANAGE])
async def get_appeal(appeal_id: uuid.UUID, db: Annotated[AsyncSession, Depends(get_db)]) -> Any:
    appeal = await service.get_appeal(db, appeal_id)
    return AppealAdminOut.model_validate(appeal, from_attributes=True)


@router.post("/appeals/{appeal_id}/status", response_model=AppealAdminOut)
async def advance_appeal_status(
    appeal_id: uuid.UUID,
    payload: AppealStatusIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(APPEALS_MANAGE))],
) -> Any:
    appeal = await service.advance_appeal_status(
        db, appeal_id, to_status=payload.to_status, actor=user
    )
    return AppealAdminOut.model_validate(appeal, from_attributes=True)


@router.post("/appeals/{appeal_id}/answer", response_model=AppealAdminOut)
async def answer_appeal(
    appeal_id: uuid.UUID,
    payload: AppealAnswerIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(APPEALS_MANAGE))],
) -> Any:
    appeal = await service.answer_appeal(db, appeal_id, answer_text=payload.answer_text, actor=user)
    return AppealAdminOut.model_validate(appeal, from_attributes=True)
