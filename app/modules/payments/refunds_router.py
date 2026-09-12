"""The manual refund (plan `03.10b-payments-reconciliation` task 9, `tz/08`,
decision #12). Mounted at the ROOT `/refunds` prefix — never `/payments/…` —
per `design/03`'s own table, which lists these four routes there and not
under the `payments` router's own `/payments` prefix `backoffice_router.py`
uses.

`POST /refunds` carries no `require_permission` gate: an applicant files for
their OWN application (ownership checked inside
`backoffice_service._may_request_refund_for`, the same split
`payments/router.py`'s own module docstring gives `GET /invoices/{id}`) and
an accountant (`payments.manage`) files for anyone. `submit-decision` and
`approve` ARE gated — the accountant's own half and the rahbar's own half of
the maker-checker `tz/08` describes, mirroring `backoffice_router.py`'s own
manual-confirmation split one permission over.

Stage 7.9 task 7 (decision #154) adds `GET /refunds/{id}` — the single-item
read `available_sources` needs — and replaces the old `budget_amount`/
`recipient_amount`/`other_amount` breakdown with `components`, built by
`backoffice_service.refund_components_out` on every response that names one
refund (never on the paged list, matching the old `allocations`/
`recipient_account`/`budget_account` fields' own scope, which the list route
never populated either).

Stage 11 (rulings R1, R3) moves the gate on both reads from the route
(`require_any_permission(PAYMENTS_VIEW, PAYMENTS_CONFIRM)`) into the service:
`backoffice_service.list_refunds`/`get_refund_for_actor` decide WHETHER an
actor may see a refund at all — staff as before, plus now the refund's own
owner (or representative), the same `_may_request_refund_for` rule
`POST /refunds` already applies — and `_refund_out` below decides WHAT they
see of it, blanking the accountant's working fields for a non-staff reader."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import xlsx
from app.core.deps import get_db
from app.core.schemas import PAGING_MAX, Page
from app.core.time import business_today
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User
from app.modules.payments import backoffice_service, export
from app.modules.payments import service as payments_service
from app.modules.payments.backoffice_schemas import (
    RefundApproveIn,
    RefundOut,
    RefundRequestIn,
    RefundSubmitDecisionIn,
)
from app.modules.payments.models import REFUND_STATUSES, Refund
from app.modules.payments.permissions import PAYMENTS_CONFIRM, PAYMENTS_MANAGE

router = APIRouter(tags=["refunds"])

_STATUS_PATTERN = "^(" + "|".join(REFUND_STATUSES) + ")$"


async def _refund_out(db: AsyncSession, row: Refund, *, staff: bool, single: bool) -> RefundOut:
    """WHAT a reader sees of a refund (stage 11, ruling R3) — the same
    "what is shown, not whether" split `router.py::_invoice_out` draws for
    `recipients`, on the same predicate. Staff (`holds_payments_read`) get
    the row entire, and on the single read (`single=True`) `components` and
    `available_sources` too, as before. A reader who is not staff — the
    refund's owner, admitted by `get_refund_for_actor`/`list_refunds` —
    gets `suggested_amount`, `suggestion_reason`, `components` and
    `available_sources` blanked: the hint is the accountant's working
    figure, and the split is the leshoz's bookkeeping; `final_amount`,
    `status` and `due_at` are the citizen's fields regardless.

    `comment` is a fourth field blanked conditionally, not always: it is
    ONE column three writers share (the citizen's own filing text, then
    `backoffice_service.submit_refund_decision`/`approve_refund`, each
    `if comment: row.comment = comment`) — once a decision has overwritten
    it, nobody can tell whose text it holds any more, so a non-staff reader
    keeps their own `comment` only while the refund is still
    `backoffice_service._STATUS_REQUESTED`; from `in_review` onward it is
    blanked too, fail-closed, the same "hide rather than mislabel" posture
    the hint already has.

    `staff` is computed ONCE per request by the caller (`payments_service.
    holds_payments_read` is not free — a permission lookup) and passed in
    rather than re-derived here per row, the same one-predicate-per-request
    shape `router.py::list_invoices`'s own caller keeps."""
    out = RefundOut.model_validate(row)
    if not staff:
        out.suggested_amount = None
        out.suggestion_reason = None
        if row.status != backoffice_service._STATUS_REQUESTED:
            out.comment = None
        return out
    if single:
        out.components = await backoffice_service.refund_components_out(db, row)
        out.available_sources = await backoffice_service.available_sources_for(db, row.invoice_id)
    return out


@router.post("/refunds", status_code=201, response_model=RefundOut)
async def request_refund(
    body: RefundRequestIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> Any:
    """An applicant appeals their own application, or an accountant files on
    anyone's behalf (ruling 7). Always 201: `suggested_amount` may be `None`
    with a `suggestion_reason` instead — a hint is never a reason to refuse
    filing (ruling 2). `components` is always `[]` here — nothing has been
    submitted yet.

    Stage 11 fix wave: the 201 echo goes through `_refund_out` too, the
    same `staff` predicate `get_refund`/`list_refunds` use — a citizen
    filing their own refund must not read the accountant's hint back off
    the very response that confirms their filing. `components` stays `[]`
    either way (nothing has been submitted yet), so this only ever changes
    `suggested_amount`/`suggestion_reason` for a non-staff filer."""
    row = await backoffice_service.request_refund(
        db,
        application_id=body.application_id,
        basis_item_id=body.basis_item_id,
        comment=body.comment,
        actor=actor,
    )
    staff = await payments_service.holds_payments_read(db, actor)
    return await _refund_out(db, row, staff=staff, single=False)


@router.post("/refunds/{refund_id}/submit-decision", response_model=RefundOut)
async def submit_refund_decision(
    refund_id: uuid.UUID,
    body: RefundSubmitDecisionIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PAYMENTS_MANAGE))],
) -> Any:
    """The accountant's own half (ruling 4): stores the breakdown by source,
    moves `requested` -> `in_review`, touches no money. A breakdown that
    does not sum to `final_amount`, or that names the same source twice
    (Override 1), answers `ERR-VAL-001` here, before any write."""
    row = await backoffice_service.submit_refund_decision(
        db,
        refund_id,
        final_amount=body.final_amount,
        components=[
            backoffice_service.RefundComponentInput(
                recipient_id=component.recipient_id, amount=component.amount
            )
            for component in body.components
        ],
        comment=body.comment,
        actor=actor,
    )
    out = RefundOut.model_validate(row)
    out.components = await backoffice_service.refund_components_out(db, row)
    return out


@router.post("/refunds/{refund_id}/approve", response_model=RefundOut)
async def approve_refund(
    refund_id: uuid.UUID,
    body: RefundApproveIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PAYMENTS_CONFIRM))],
) -> Any:
    """The rahbar's own half: `resolution="returned"` writes the negative
    `allocations` entries and moves the refund to `returned`;
    `resolution="rejected"` writes nothing and moves it to `rejected`.
    Neither ever touches the invoice or the application (ruling 6).

    The response's `components` carries each source's own name and
    resolved account (Override 3 — this used to read
    `allocation.target == "budget"`, a value migration `0046` retired; the
    generalised read is `component.recipient_id is None` for the leshoz's
    own remainder, done inside `refund_components_out` rather than here)."""
    approved = await backoffice_service.approve_refund(
        db,
        refund_id,
        resolution=body.resolution,
        comment=body.comment,
        actor=actor,
    )
    out = RefundOut.model_validate(approved.refund)
    out.components = await backoffice_service.refund_components_out(db, approved.refund)
    return out


@router.get("/refunds/export.xlsx")
async def export_refunds_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
    lang: xlsx.Lang = "uz_latn",
    application_id: uuid.UUID | None = None,
    status: Annotated[str | None, Query(pattern=_STATUS_PATTERN)] = None,
) -> Response:
    """`GET /refunds` as a spreadsheet (stage 13, ruling #204): the same
    reader set as the list (staff see their zone, a citizen their own rows
    with the accountant's fields blanked — stage 11, ruling R3), the same
    `application_id`/`?status=` filters, every matching row up to the cap.
    Declared BEFORE `/refunds/{refund_id}` on purpose — `export.xlsx` is
    not a UUID, and the two share the same path-segment count."""
    items, total, cap = await export.refund_rows(
        db, actor=actor, lang=lang, application_id=application_id, status=status
    )
    filename = f"qaytarishlar-{business_today().isoformat()}.xlsx"
    rendered = export.render_refunds(items, lang=lang)
    return xlsx.xlsx_response(rendered, filename=filename, total=total, cap=cap)


@router.get("/refunds/{refund_id}", response_model=RefundOut)
async def get_refund(
    refund_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> Any:
    """The single-item read (stage 7.9 task 7): `available_sources` — the
    invoice's own frozen split, so the accountant's/rahbar's own form
    offers exactly the parties THIS payment was split between — and
    `components`, whatever has already been submitted.

    Gated on `PAYMENTS_VIEW` OR `PAYMENTS_CONFIRM` (whole-branch review
    Important 3, fixed from `PAYMENTS_VIEW` alone): the rahbar
    (`executor_head`, `payments.confirm`) is exactly who this docstring's
    own "rahbar's own form" refers to, and under the narrower gate he could
    reach `POST .../approve` (which returns `components` too) but not THIS
    route — reading the breakdown only by committing to it. `available_
    sources` existed for the actor it was unreachable to.

    Stage 11 (ruling R3) opens this read to the refund's owner —
    `get_refund_for_actor` decides whether, `_refund_out` decides what."""
    row = await backoffice_service.get_refund_for_actor(db, refund_id, actor=actor)
    staff = await payments_service.holds_payments_read(db, actor)
    return await _refund_out(db, row, staff=staff, single=True)


@router.get("/refunds", response_model=Page[RefundOut])
async def list_refunds(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
    application_id: uuid.UUID | None = None,
    status: Annotated[str | None, Query(pattern=_STATUS_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=PAGING_MAX)] = 0,
) -> Any:
    """The accountant's/rahbar's own register — every refund, optionally
    narrowed by `application_id` or `status`. `components`/
    `available_sources` stay `[]` on every row here, the same scope the old
    `allocations`/`recipient_account`/`budget_account` fields had: a page of
    up to 200 rows is not the place for a per-row extra query, and
    `GET /refunds/{id}` is the single-item read built for it.

    Stage 11 (ruling R1): the gate moved off this route entirely — see
    `backoffice_service.list_refunds`'s own docstring. Staff
    (`payments.view` or `.confirm`, the same actor `get_refund` above
    admits) still get the register; anyone else gets their own refunds,
    never a 403, and the accounting fields blanked (`_refund_out`)."""
    staff = await payments_service.holds_payments_read(db, actor)
    rows, total = await backoffice_service.list_refunds(
        db,
        application_id=application_id,
        status=status,
        limit=limit,
        offset=offset,
        actor=actor,
    )
    return Page[RefundOut](
        items=[await _refund_out(db, row, staff=staff, single=False) for row in rows],
        total=total,
        page=offset // limit + 1,
        page_size=limit,
    )
