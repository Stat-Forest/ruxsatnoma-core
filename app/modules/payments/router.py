"""Read routes over `invoices` — `GET /invoices/{id}` and `GET
/invoices?application_id=`. No write route in this task: an invoice is
issued and cancelled only by `subscribers.py`, in reaction to an
`applications` event, never by a direct client action. Task 3+ adds the
provider-facing write surface (Payme's `PerformTransaction` and friends).

No `require_permission` gate on either route: a `payments.view` holder sees
any invoice, but so must a plain applicant reading their OWN — the
authorization decision lives inside `service.py`, exactly like
`notifications.service.mark_read`."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.schemas import Page
from app.modules.auth.deps import get_current_user
from app.modules.auth.models import User
from app.modules.payments import service
from app.modules.payments.schemas import InvoiceOut

router = APIRouter(tags=["payments"])


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> Any:
    return await service.get_invoice_for_actor(db, invoice_id, actor=actor)


@router.get("/invoices", response_model=Page[InvoiceOut])
async def list_invoices(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    items, total = await service.list_invoices_for_actor(
        db, application_id, actor=actor, limit=limit, offset=offset
    )
    return Page[InvoiceOut](
        items=[InvoiceOut.model_validate(item) for item in items],
        total=total,
        page=offset // limit + 1,
        page_size=limit,
    )
