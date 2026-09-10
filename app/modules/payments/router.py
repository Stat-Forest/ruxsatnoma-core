"""Read routes over `invoices` — `GET /invoices/{id}` and `GET /invoices`
(with or without `?application_id=`) — plus `POST /invoices/{id}/pay-intents`
(task 5), our own side of starting a payment. An invoice itself is issued and
cancelled only by `subscribers.py`, in reaction to an `applications` event,
never by a direct client action.

No `require_permission` gate on any of the three routes: a `payments.view`
holder acts on any invoice, but so must a plain applicant (or an effective
representative) acting on their OWN — the authorization decision lives
inside `service.py`, exactly like `notifications.service.mark_read`.

`GET /invoices` without `?application_id=` is the register itself
(backend-gaps finding 3): before this, only a filtered or by-id lookup
existed, so an accountant's screen could show one application's invoices but
never browse the whole book. That path is staff (`holds_payments_read`) and
zone-scoped (decision #70); anyone else gets their own invoices instead
(stage 11, ruling R1) — `service.list_invoices_for_actor`'s own docstring
carries the reasoning; this router stays a thin pass-through for both.

Stage 7.9 task 8: `GET /invoices/{id}` additionally attaches `recipients`
(`_invoice_out` below) for a STAFF reader ONLY — gated on `service.
holds_payments_read` (`payments.view` OR `payments.confirm`, whole-branch
review Important 2), the SAME predicate `_may_act_on_invoices_of`'s staff
branch already uses to decide who may act on the invoice at all. Using a
narrower predicate here would silently drop `recipients` for an actor the
route otherwise treats as staff — the same authorization split as
everything else in this file, just answering a different question (WHAT is
shown, not WHETHER the invoice is). `GET /invoices` (the list route) never
attaches it, the same scope `RefundOut.available_sources` draws for
itself."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.idempotency import IdempotencyContext
from app.core.schemas import PAGING_MAX, Page
from app.modules.auth.deps import get_current_user, idempotency_context
from app.modules.auth.models import User
from app.modules.payments import service
from app.modules.payments.models import INVOICE_STATUSES, Invoice
from app.modules.payments.schemas import InvoiceOut, InvoiceRecipientOut, PayIntentIn, PayIntentOut

router = APIRouter(tags=["payments"])

_INVOICE_STATUS_PATTERN = "^(" + "|".join(INVOICE_STATUSES) + ")$"


async def _invoice_out(db: AsyncSession, invoice: Invoice, *, actor: User) -> Any:
    """Builds `GET /invoices/{id}`'s response, attaching `recipients` ONLY
    for a STAFF reader (Override 4, stage 7.9 task 8 — who receives the
    money is internal allocation, never part of what a citizen is paying
    for). Everyone else gets `recipients` OMITTED from the JSON body
    entirely — `exclude={"recipients"}`, never a blanket `exclude_none`
    (`InvoiceOut.recipients`'s own docstring says why only this one field
    is allowed to disappear).

    Gated on `service.holds_payments_read`, NOT `holds_payments_view`
    (whole-branch review Important 2, fixed after `executor_head` — the
    checker half of the maker-checker PAID, and the head of the leshoz
    that receives the invoice's own remainder — reached this route (via
    `_may_act_on_invoices_of`'s `holds_payments_read`-gated staff branch)
    and got a body with `recipients` silently absent, not a 403 and not a
    null). One access rule, one source: whoever the route already treats
    as staff must see the same thing every other staff reader does."""
    out = InvoiceOut.model_validate(invoice)
    out.settled_by_benefit = await service.is_settled_by_benefit(db, invoice)
    if not await service.holds_payments_read(db, actor):
        return JSONResponse(out.model_dump(mode="json", exclude={"recipients"}))
    out.recipients = [
        InvoiceRecipientOut.model_validate(row)
        for row in await service.invoice_recipients(db, invoice.id)
    ]
    return out


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> Any:
    invoice = await service.get_invoice_for_actor(db, invoice_id, actor=actor)
    return await _invoice_out(db, invoice, actor=actor)


@router.get("/invoices", response_model=Page[InvoiceOut])
async def list_invoices(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
    # Optional (backend-gaps finding 3): given, this is the original
    # application-scoped read; omitted, it is the register itself, gated in
    # the service to `payments.view` holders only — see this file's own
    # module docstring and `service.list_invoices_for_actor`'s.
    application_id: uuid.UUID | None = None,
    status: Annotated[str | None, Query(pattern=_INVOICE_STATUS_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    # `le=PAGING_MAX` like every other paged route: `offset` is bound into SQL as a
    # bigint, so an unbounded one reaches asyncpg as `DataError: value out of int64
    # range` — a 500 for a query string anybody can type. Surfaced by merging 3.11a,
    # whose `test_every_integer_query_parameter_carries_an_upper_bound` did not exist
    # when this route was written.
    offset: Annotated[int, Query(ge=0, le=PAGING_MAX)] = 0,
) -> Any:
    items, total = await service.list_invoices_for_actor(
        db, application_id, actor=actor, status=status, limit=limit, offset=offset
    )
    # `service.list_invoices_for_actor` is itself unaffected by ruling #185
    # (plan B4) — the derivation is per-row and cheap (`is_settled_by_
    # benefit`'s own docstring: no query at all unless a row is actually
    # `paid` and zero), so it is applied here, the same two-step shape
    # `_invoice_out` above uses for a single invoice.
    out_items = []
    for item in items:
        out = InvoiceOut.model_validate(item)
        out.settled_by_benefit = await service.is_settled_by_benefit(db, item)
        out_items.append(out)
    return Page[InvoiceOut](
        items=out_items,
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
    """Refuses with, in the order the guards run: **`ERR-SYS-003`** (404) when
    the invoice does not exist, its application does not, **or the caller may
    not act on it** — the three are deliberately indistinguishable, so a
    stranger cannot probe which invoice ids exist; **`ERR-PAY-004`** (409) when
    the invoice is not `pending`; **`ERR-PAY-002`** when it is past `due_at`;
    and **`ERR-PAY-007`** (409) when the split cannot be routed at the provider
    — some receiver frozen onto this invoice has no `payme_account_id`, so
    under decision #160 the payment is refused rather than taken onto the
    Agency's cashbox for somebody to move by hand. `details.missing` names the
    offending receivers by `position` only; the names are in the server log,
    not in a body a citizen reads.

    These codes are listed here because a route's docstring is the only thing
    that carries them into the served OpenAPI — `ERR-PAY-007` was invisible to
    anyone reading the schema until this sentence existed.

    `Idempotency-Key` is MANDATORY (3.4's mechanism, ruling: ours, on our
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
