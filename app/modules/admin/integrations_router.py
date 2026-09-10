"""Admin surface over the outbox/DLQ (plan 03.4 ruling 4): integrations is
level 0 and cannot host permission-gated HTTP itself; operating the queue is
an adminka function, so the router lives in admin.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import xlsx
from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.core.time import business_today
from app.modules.admin import export
from app.modules.admin.permissions import INTEGRATIONS_MANAGE, INTEGRATIONS_VIEW
from app.modules.auth.deps import require_any_permission, require_permission
from app.modules.auth.models import User
from app.modules.integrations import repo, service

router = APIRouter(prefix="/admin/integrations", tags=["admin"])


class OutboxMessageOut(BaseModel):
    """`OutboxMessage` minus `payload` — it may carry OTP codes, so listings never
    expose it; inspect a payload directly in the DB when needed."""

    id: uuid.UUID
    destination: str
    status: str
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    correlation_id: str | None
    created_at: datetime
    delivered_at: datetime | None


class DeadLetterOut(BaseModel):
    """`InboundDeadLetter` minus `payload`, for the same reason as `OutboxMessageOut`."""

    id: uuid.UUID
    source: str
    error: str
    status: str
    received_at: datetime
    processed_by: uuid.UUID | None
    processed_at: datetime | None


@router.get("/outbox", response_model=Page[OutboxMessageOut])
async def list_outbox(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[PageParams, Depends()],
    actor: Annotated[User, Depends(require_any_permission(INTEGRATIONS_VIEW, INTEGRATIONS_MANAGE))],
    status: str | None = None,
    destination: str | None = None,
) -> Page[OutboxMessageOut]:
    rows, total = await repo.list_outbox(
        db, status=status, destination=destination, page=params.page, page_size=params.page_size
    )
    return Page[OutboxMessageOut](
        items=[OutboxMessageOut.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/outbox/export.xlsx")
async def export_outbox_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(INTEGRATIONS_VIEW, INTEGRATIONS_MANAGE))],
    lang: xlsx.Lang = "uz_latn",
    status: str | None = None,
    destination: str | None = None,
) -> Response:
    """`GET /admin/integrations/outbox` as a spreadsheet (stage 13, ruling
    #204): the same filters, the same view-or-manage gate, every matching
    row up to the configured cap. `payload` is never exported — same
    withholding `OutboxMessageOut` already applies to the JSON response."""
    items, total, cap = await export.outbox_rows(
        db, lang=lang, status=status, destination=destination
    )
    filename = f"chiquvchi-navbat-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render_outbox(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.post("/outbox/{message_id}/requeue", response_model=OutboxMessageOut)
async def requeue_outbox_message(
    message_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(INTEGRATIONS_MANAGE))],
) -> OutboxMessageOut:
    row = await service.requeue_message(
        db, message_id, actor_id=actor.id, ip=request.client.host if request.client else None
    )
    return OutboxMessageOut.model_validate(row, from_attributes=True)


@router.get("/dead-letters", response_model=Page[DeadLetterOut])
async def list_dead_letters(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[PageParams, Depends()],
    actor: Annotated[User, Depends(require_any_permission(INTEGRATIONS_VIEW, INTEGRATIONS_MANAGE))],
    status: str | None = None,
) -> Page[DeadLetterOut]:
    rows, total = await repo.list_dead_letters(
        db, status=status, page=params.page, page_size=params.page_size
    )
    return Page[DeadLetterOut](
        items=[DeadLetterOut.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/dead-letters/export.xlsx")
async def export_dead_letters_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(INTEGRATIONS_VIEW, INTEGRATIONS_MANAGE))],
    lang: xlsx.Lang = "uz_latn",
    status: str | None = None,
) -> Response:
    """`GET /admin/integrations/dead-letters` as a spreadsheet (stage 13,
    ruling #204): the same filter, the same view-or-manage gate, every
    matching row up to the configured cap. `payload` is never exported —
    same withholding `DeadLetterOut` already applies to the JSON response."""
    items, total, cap = await export.dead_letters_rows(db, lang=lang, status=status)
    filename = f"kiruvchi-xatolar-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render_dead_letters(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.post("/dead-letters/{letter_id}/discard", response_model=DeadLetterOut)
async def discard_dead_letter(
    letter_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(INTEGRATIONS_MANAGE))],
) -> DeadLetterOut:
    row = await service.discard_dead_letter(
        db, letter_id, actor_id=actor.id, ip=request.client.host if request.client else None
    )
    return DeadLetterOut.model_validate(row, from_attributes=True)
