"""The user's own in-app inbox. In-app rows only (SMS/e-mail rows are the delivery
history of the same event, not separate bell entries)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import xlsx
from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.core.time import business_today
from app.modules.auth.deps import get_current_user
from app.modules.auth.models import User
from app.modules.notifications import export, service
from app.modules.notifications.schemas import MarkAllReadOut, NotificationOut, UnreadCountOut

router = APIRouter(prefix="/notifications", tags=["notifications"])


# Declared before /{notification_id} so the literal paths win the match.
@router.get("/unread-count", response_model=UnreadCountOut)
async def get_unread_count(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> UnreadCountOut:
    return UnreadCountOut(count=await service.unread_count(db, user.id))


@router.post("/read-all", response_model=MarkAllReadOut)
async def mark_all_read(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> MarkAllReadOut:
    return MarkAllReadOut(updated=await service.mark_all_read(db, user.id))


@router.get("", response_model=Page[NotificationOut])
async def list_notifications(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[PageParams, Depends()],
    user: Annotated[User, Depends(get_current_user)],
    unread: bool = False,
) -> Page[NotificationOut]:
    rows, total = await service.list_inbox(
        db, user_id=user.id, unread_only=unread, page=params.page, page_size=params.page_size
    )
    return Page[NotificationOut](
        items=[NotificationOut.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/export.xlsx")
async def export_notifications_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    lang: xlsx.Lang = "uz_latn",
    unread: bool = False,
) -> Response:
    """`GET /notifications` as a spreadsheet (stage 13, ruling #204): the
    caller's own inbox, the same `unread` filter, every matching row up to
    the configured cap. Declared before `/{notification_id}` on purpose."""
    items, total, cap = await export.rows(db, actor=user, lang=lang, unread=unread)
    filename = f"bildirishnomalar-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> NotificationOut:
    row = await service.mark_read(db, notification_id, user_id=user.id)
    return NotificationOut.model_validate(row, from_attributes=True)
