"""Public FAQ read (anonymous, rate-limited) and support tickets (any
authenticated user; assign/resolve gated by `help.tickets.manage`). FAQ
writes live in `admin_router.py`, mirroring `admin.announcements_router`'s
own `router`/`admin_router` split."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import xlsx
from app.core.deps import get_db
from app.core.ratelimit import rate_limit
from app.core.schemas import Page, PageParams
from app.core.time import business_today
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User
from app.modules.help import export, service
from app.modules.help.permissions import TICKETS_MANAGE
from app.modules.help.schemas import (
    FaqOut,
    TicketAssignIn,
    TicketIn,
    TicketMessageIn,
    TicketMessageOut,
    TicketOut,
    TicketWithMessagesOut,
)

router = APIRouter(prefix="/help", tags=["help"])

_FAQ_READ_LIMIT = Depends(rate_limit("public_help_faq", "ratelimit_public_help_faq_per_minute"))


@router.get("/faq", response_model=list[FaqOut], dependencies=[_FAQ_READ_LIMIT])
async def public_faq(
    db: Annotated[AsyncSession, Depends(get_db)], category: str | None = None
) -> Any:
    return await service.list_public_faq(db, category=category)


@router.post("/tickets", response_model=TicketOut, status_code=201)
async def create_ticket(
    payload: TicketIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> Any:
    return await service.create_ticket(
        db, subject=payload.subject, body=payload.body, file_id=payload.file_id, actor=user
    )


@router.get("/tickets", response_model=Page[TicketOut])
async def list_tickets(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    params: Annotated[PageParams, Depends()],
    status: str | None = None,
) -> Any:
    items, total = await service.list_tickets(db, actor=user, status=status, params=params)
    return Page[TicketOut](
        items=[TicketOut.model_validate(x, from_attributes=True) for x in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/tickets/export.xlsx")
async def export_tickets_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    lang: xlsx.Lang = "uz_latn",
    status: str | None = None,
) -> Response:
    """`GET /tickets` as a spreadsheet (stage 13, ruling #204): the same
    scope, the same filter, every matching row up to the configured cap.
    Declared before `/tickets/{ticket_id}` on purpose — `export.xlsx` is not
    a UUID, and a 404 here beats the 422 the UUID parser would answer."""
    items, total, cap = await export.ticket_rows(db, actor=user, lang=lang, status=status)
    filename = f"support-tickets-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render_tickets(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.get("/tickets/{ticket_id}", response_model=TicketWithMessagesOut)
async def get_ticket(
    ticket_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> Any:
    ticket, messages = await service.get_ticket(db, ticket_id, actor=user)
    return TicketWithMessagesOut(
        **TicketOut.model_validate(ticket, from_attributes=True).model_dump(),
        messages=[TicketMessageOut.model_validate(m, from_attributes=True) for m in messages],
    )


@router.post("/tickets/{ticket_id}/messages", response_model=TicketMessageOut, status_code=201)
async def add_message(
    ticket_id: uuid.UUID,
    payload: TicketMessageIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> Any:
    return await service.add_message(
        db, ticket_id, body=payload.body, file_id=payload.file_id, actor=user
    )


@router.post("/tickets/{ticket_id}/assign", response_model=TicketOut)
async def assign_ticket(
    ticket_id: uuid.UUID,
    payload: TicketAssignIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(TICKETS_MANAGE))],
) -> Any:
    return await service.assign_ticket(db, ticket_id, assignee_id=payload.assignee_id, actor=user)


@router.post("/tickets/{ticket_id}/resolve", response_model=TicketOut)
async def resolve_ticket(
    ticket_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(TICKETS_MANAGE))],
) -> Any:
    return await service.resolve_ticket(db, ticket_id, actor=user)


@router.post("/tickets/{ticket_id}/close", response_model=TicketOut)
async def close_ticket(
    ticket_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> Any:
    return await service.close_ticket(db, ticket_id, actor=user)
