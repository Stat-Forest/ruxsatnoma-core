"""Read routes over `invoices` — `GET /invoices/{id}` and `GET
/invoices?application_id=` — plus `POST /invoices/{id}/pay-intents` (task 5),
our own side of starting a payment. An invoice itself is issued and
cancelled only by `subscribers.py`, in reaction to an `applications` event,
never by a direct client action.

No `require_permission` gate on any of the three routes: a `payments.view`
holder acts on any invoice, but so must a plain applicant (or an effective
representative) acting on their OWN — the authorization decision lives
inside `service.py`, exactly like `notifications.service.mark_read`."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.idempotency import IdempotencyContext
from app.core.schemas import PAGING_MAX, Page
from app.modules.auth.deps import get_current_user, idempotency_context
from app.modules.auth.models import User
from app.modules.payments import service
from app.modules.payments.schemas import InvoiceOut, PayIntentIn, PayIntentOut

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
    # `le=PAGING_MAX` like every other paged route: `offset` is bound into SQL as a
    # bigint, so an unbounded one reaches asyncpg as `DataError: value out of int64
    # range` — a 500 for a query string anybody can type. Surfaced by merging 3.11a,
    # whose `test_every_integer_query_parameter_carries_an_upper_bound` did not exist
    # when this route was written.
    offset: Annotated[int, Query(ge=0, le=PAGING_MAX)] = 0,
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


@router.post("/invoices/{invoice_id}/pay-intents", response_model=PayIntentOut, status_code=201)
async def create_pay_intent(
    invoice_id: uuid.UUID,
    body: PayIntentIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
    ctx: Annotated[IdempotencyContext, Depends(idempotency_context)],
) -> Any:
    """`Idempotency-Key` is MANDATORY (3.4's mechanism, ruling: ours, on our
    own route — never on `/webhooks/payme`, which has Payme's own). `ctx`
    is declared after `actor` (mirrors `gis/imports_router.py::create_import`)
    so the SAME `get_current_user` call both depend on is resolved once;
    `ctx.save()` runs before the response so a replay of the same key
    returns the stored 201 instead of a second `payment_intents` row."""
    _intent, payment_url = await service.create_pay_intent(
        db, invoice_id, provider=body.provider, actor=actor, idempotency_key=ctx.key
    )
    out = PayIntentOut(payment_url=payment_url)
    await ctx.save(db, status_code=201, body=out.model_dump(mode="json"))
    return out
