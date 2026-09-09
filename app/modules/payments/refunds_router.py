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
read `available_sources` needs, gated `PAYMENTS_VIEW` like the list route —
and replaces the old `budget_amount`/`recipient_amount`/`other_amount`
breakdown with `components`, built by `backoffice_service.
refund_components_out` on every response that names one refund (never on
the paged list, matching the old `allocations`/`recipient_account`/
`budget_account` fields' own scope, which the list route never populated
either)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.schemas import PAGING_MAX, Page
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User
from app.modules.payments import backoffice_service
from app.modules.payments.backoffice_schemas import (
    RefundApproveIn,
    RefundOut,
    RefundRequestIn,
    RefundSubmitDecisionIn,
)
from app.modules.payments.models import REFUND_STATUSES
from app.modules.payments.permissions import PAYMENTS_CONFIRM, PAYMENTS_MANAGE, PAYMENTS_VIEW

router = APIRouter(tags=["refunds"])

_STATUS_PATTERN = "^(" + "|".join(REFUND_STATUSES) + ")$"


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
    submitted yet."""
    row = await backoffice_service.request_refund(
        db,
        application_id=body.application_id,
        basis_item_id=body.basis_item_id,
        comment=body.comment,
        actor=actor,
    )
    return RefundOut.model_validate(row)


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


@router.get("/refunds/{refund_id}", response_model=RefundOut)
async def get_refund(
    refund_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PAYMENTS_VIEW))],
) -> Any:
    """The single-item read (stage 7.9 task 7): `available_sources` — the
    invoice's own frozen split, so the accountant's/rahbar's own form
    offers exactly the parties THIS payment was split between — and
    `components`, whatever has already been submitted."""
    row = await backoffice_service.get_refund(db, refund_id)
    out = RefundOut.model_validate(row)
    out.components = await backoffice_service.refund_components_out(db, row)
    out.available_sources = await backoffice_service.available_sources_for(db, row.invoice_id)
    return out


@router.get("/refunds", response_model=Page[RefundOut])
async def list_refunds(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PAYMENTS_VIEW))],
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
    `GET /refunds/{id}` is the single-item read built for it."""
    rows, total = await backoffice_service.list_refunds(
        db,
        application_id=application_id,
        status=status,
        limit=limit,
        offset=offset,
        actor=actor,
    )
    return Page[RefundOut](
        items=[RefundOut.model_validate(row) for row in rows],
        total=total,
        page=offset // limit + 1,
        page_size=limit,
    )
