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
never populated either). Both reads are gated `PAYMENTS_VIEW` OR
`PAYMENTS_CONFIRM` (whole-branch review Important 3) — the rahbar
(`executor_head`) approves through `PAYMENTS_CONFIRM` alone and needs both
to see what he is approving before he commits to it."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import xlsx
from app.core.deps import get_db
from app.core.schemas import PAGING_MAX, Page
from app.core.time import business_today
from app.modules.auth.deps import get_current_user, require_any_permission, require_permission
from app.modules.auth.models import User
from app.modules.payments import backoffice_service, export
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


@router.get("/refunds/export.xlsx")
async def export_refunds_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(PAYMENTS_VIEW, PAYMENTS_CONFIRM))],
    lang: xlsx.Lang = "uz_latn",
    application_id: uuid.UUID | None = None,
    status: Annotated[str | None, Query(pattern=_STATUS_PATTERN)] = None,
) -> Response:
    """`GET /refunds` as a spreadsheet (stage 13, ruling #204): the same
    `payments.view`-or-`payments.confirm` gate, the same `application_id`/
    `?status=` filters, every matching row up to the configured cap.
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
    actor: Annotated[User, Depends(require_any_permission(PAYMENTS_VIEW, PAYMENTS_CONFIRM))],
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
    sources` existed for the actor it was unreachable to."""
    row = await backoffice_service.get_refund(db, refund_id)
    out = RefundOut.model_validate(row)
    out.components = await backoffice_service.refund_components_out(db, row)
    out.available_sources = await backoffice_service.available_sources_for(db, row.invoice_id)
    return out


@router.get("/refunds", response_model=Page[RefundOut])
async def list_refunds(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(PAYMENTS_VIEW, PAYMENTS_CONFIRM))],
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

    Widened to `PAYMENTS_VIEW` OR `PAYMENTS_CONFIRM` alongside `get_refund`
    above (whole-branch review Important 3): this docstring already called
    it the rahbar's own register too, and there is no other route through
    which he could ever discover a refund's id to approve it — no
    notification carries one today (`submit_refund_decision` sends none)."""
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
