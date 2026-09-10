"""The accountant's back office (plan `03.10b-payments-reconciliation`). Its
first two routes are the bank-statement import: the multipart upload and the
status read the accountant polls afterwards. Task 5 adds the discrepancy
register itself: `GET /payments/reconciliations` and `POST
/payments/reconciliations/{id}/resolve`.

All four are permission-gated and none is zone-scoped — a bank statement, and
the register built from it, belong to the accounting department and carry no
`organization_id` there would be anything to scope on. The upload and a
resolution are `payments.manage` (the accountant's own actions); the two reads
are `payments.view`.

**There is no task table, and this stage creates none.** `tz/08` asks for "a
task for the accountant" beside the register — an OPEN `reconciliations` row
with `assigned_to` set IS that task, exactly what `design/02` gives the
column for. Do not build a second worklist on top of this one.

**What this register answers, and what it never can.** It answers "did the
money arrive against an invoice" — a `matched`/`discrepancy`/`unknown` row per
bank line, or one period row per provider settlement. It can never answer
"did a configured receiver's own wallet, or the leshoz's own remainder,
actually reach its account": a `payment_recipients` row is a Payme WALLET
(`payme_account_id`), never a bank account, so every receiver row's
`allocations.account` is structurally `NULL`, and a leshoz's own
`requisites` may legitimately carry no `"account"` key either (`tz/12`
#15) — so most of every `allocations` row has `account = NULL`. `matcher.py`'s
module docstring states the same limitation for the matching side; this is
the same fact, read from the accountant's own register rather than from the
code that filled it in.

`POST /payments/bank-statements` requires an `Idempotency-Key`: without it a
double-clicked or retried upload files a SECOND statement, whose lines then
duplicate the whole month in the register with no delete path to undo it. On
this route `ERR-SYS-005` means an Idempotency-Key CONFLICT and nothing else —
this module's own state conflicts are `ERR-PAY-004`/`ERR-PAY-005`.

The body is capped BEFORE it exists as one `bytes` object (lesson: «a cap
checked after reading the body is not a cap»): `read_capped` rejects an
oversized `Content-Length` without reading a byte and otherwise streams in
chunks, aborting the instant the running total passes `bank_statement_max_mb`.
`save_upload` re-checks as the final authority, against this module's OWN MIME
table (`statement_service.STATEMENT_UPLOAD_TYPES`) rather than the document
whitelist `POST /files` uses.
"""

import json
import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files, settings_store, xlsx
from app.core.deps import get_db
from app.core.errors import err
from app.core.idempotency import IdempotencyContext
from app.core.schemas import PAGING_MAX, Page
from app.core.time import business_today
from app.modules.auth.deps import idempotency_context, require_any_permission, require_permission
from app.modules.auth.models import User
from app.modules.payments import backoffice_service, export, statement_service
from app.modules.payments.backoffice_schemas import (
    AllocationOut,
    FiledManualConfirmationOut,
    ManualConfirmationIn,
    ManualConfirmationOut,
    ManualConfirmationRejectIn,
    ReconciliationOut,
    ReconciliationResolveIn,
    StatementAccepted,
    StatementLineOut,
    StatementListItem,
    StatementOut,
)
from app.modules.payments.models import (
    BANK_STATEMENT_STATUSES,
    MANUAL_CONFIRMATION_STATUSES,
    RECONCILIATION_STATUSES,
)
from app.modules.payments.permissions import PAYMENTS_CONFIRM, PAYMENTS_MANAGE, PAYMENTS_VIEW
from app.modules.payments.statement_parser import OPTIONAL_FIELDS, REQUIRED_FIELDS

router = APIRouter(prefix="/payments", tags=["payments"])

_KNOWN_FIELDS = frozenset(REQUIRED_FIELDS) | frozenset(OPTIONAL_FIELDS)
# Derived from the model's own tuple, never retyped: a status added to
# `RECONCILIATION_STATUSES` widens what this query parameter accepts with no
# second edit required here.
_STATUS_PATTERN = "^(" + "|".join(RECONCILIATION_STATUSES) + ")$"
_MANUAL_CONFIRMATION_STATUS_PATTERN = "^(" + "|".join(MANUAL_CONFIRMATION_STATUSES) + ")$"
_STATEMENT_STATUS_PATTERN = "^(" + "|".join(BANK_STATEMENT_STATUSES) + ")$"


def _parse_column_map(raw: str) -> dict[str, str]:
    """`column_map` arrives as a JSON STRING inside the multipart body — a form
    field cannot carry a nested object — so it is decoded here, at the transport
    edge, and validated to be the flat `{our field: the file's column}` map the
    parser expects (the same shape and the same reasoning as
    `gis/imports_router._parse_attributes`).

    Anything else is `ERR-VAL-001` rather than a 500 later in the job: this
    value is stored in a JSONB column and then read as column names, so a list,
    a nested object or a number would fail far from the request that supplied
    it. Unknown keys are refused too — there is no contracted bank format, so a
    typo'd field name is the single likeliest mistake an accountant makes, and
    silently ignoring it would produce a statement that imports and matches
    nothing.
    """
    try:
        parsed = json.loads(raw or "{}")
    # Deliberately parenthesized, not the PEP 758 bare form (see
    # core.settings_store.coerce for the reasoning).
    except (ValueError, TypeError):  # fmt: skip
        raise err("ERR-VAL-001", details={"reason": "column_map_not_json"}) from None
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise err("ERR-VAL-001", details={"reason": "column_map_not_a_string_map"})
    unknown = sorted(set(parsed) - _KNOWN_FIELDS)
    if unknown:
        raise err("ERR-VAL-001", details={"reason": "column_map_unknown_fields", "fields": unknown})
    return parsed


@router.post("/bank-statements", status_code=202)
async def create_bank_statement(
    request: Request,
    file: UploadFile,
    statement_date: Annotated[date, Form()],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(PAYMENTS_MANAGE))],
    ctx: Annotated[IdempotencyContext, Depends(idempotency_context)],
    column_map: Annotated[str, Form()] = "{}",
) -> StatementAccepted:
    """202, not 201: the file is stored and the work is QUEUED. The parse and
    the matching happen in `app/workers/jobs.py::process_bank_statements`.

    `ctx` is declared after `user` so the permission check runs first and an
    unauthorized caller never mints an idempotency marker row; `ctx.save()` runs
    before the response so a replay of the same key returns the stored 202 with
    the ORIGINAL statement id instead of queueing a duplicate.
    """
    # The column map is decoded FIRST, before a byte reaches MinIO: `save_upload`
    # writes the object before the DB flush by design, so a request rejected
    # after it leaves an orphaned object behind that nothing will ever reference
    # or clean up. The cheap, purely-syntactic check goes ahead of the expensive,
    # side-effecting one.
    parsed_map = _parse_column_map(column_map)
    cap_bytes = await settings_store.get_int(db, "bank_statement_max_mb") * 1024 * 1024
    data = await files.read_capped(file, cap_bytes, files.declared_length(request.headers))
    stored = await files.save_upload(
        db,
        data=data,
        filename=files.sanitize_filename(file.filename or "statement.csv"),
        content_type=file.content_type or "application/octet-stream",
        actor=user,
        allowed=statement_service.STATEMENT_UPLOAD_TYPES,
        cap_key="bank_statement_max_mb",
    )
    row = await statement_service.create_statement(
        db,
        file_id=stored.id,
        statement_date=statement_date,
        column_map=parsed_map,
        actor=user,
    )
    accepted = StatementAccepted(id=row.id, status=row.status)
    await ctx.save(db, status_code=202, body=accepted.model_dump(mode="json"))
    return accepted


@router.get("/bank-statements", response_model=Page[StatementListItem])
async def list_bank_statements(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_permission(PAYMENTS_VIEW))],
    status: Annotated[str | None, Query(pattern=_STATEMENT_STATUS_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=PAGING_MAX)] = 0,
) -> Any:
    """The register itself (backend-gaps finding 3): every import, newest
    first, `?status=` narrowing to one. Headers only, no lines — `GET
    /bank-statements/{id}` below is where those live. No zone scoping, same
    reasoning as that route: a bank statement belongs to the accounting
    department, not to a leshoz."""
    rows, total = await statement_service.list_statements(
        db, status=status, limit=limit, offset=offset
    )
    return Page[StatementListItem](
        items=[StatementListItem.model_validate(row) for row in rows],
        total=total,
        page=offset // limit + 1,
        page_size=limit,
    )


@router.get("/bank-statements/export.xlsx")
async def export_bank_statements_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_permission(PAYMENTS_VIEW))],
    lang: xlsx.Lang = "uz_latn",
    status: Annotated[str | None, Query(pattern=_STATEMENT_STATUS_PATTERN)] = None,
) -> Response:
    """`GET /payments/bank-statements` as a spreadsheet (stage 13, ruling
    #204): the same `payments.view` gate and the same `?status=` filter,
    every matching import up to the configured cap. Declared BEFORE
    `/bank-statements/{statement_id}` on purpose — `export.xlsx` is not a
    UUID, and the two share the same path-segment count."""
    items, total, cap = await export.statement_rows(db, lang=lang, status=status)
    filename = f"bank-hisobotlari-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render_statements(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.get("/bank-statements/{statement_id}", response_model=StatementOut)
async def get_bank_statement(
    statement_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_permission(PAYMENTS_VIEW))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    # `le=PAGING_MAX` like every other paged route: `offset` is bound into SQL
    # as a bigint, so an unbounded one reaches asyncpg as `DataError: value out
    # of int64 range` — a 500 for a query string anybody can type
    # (`tests/test_code_conventions.py` enforces the bound).
    offset: Annotated[int, Query(ge=0, le=PAGING_MAX)] = 0,
) -> Any:
    """The statement's own status, `stats` and `error_report`, plus a page of
    its lines in file order — what an accountant polls after the 202 and then
    works down."""
    row, lines, total = await statement_service.get_statement(
        db, statement_id, limit=limit, offset=offset
    )
    # Built field by field rather than `model_validate(row)` plus a patch: the
    # two list fields have no counterpart on the ORM row at all, and pydantic's
    # `update=` belongs to `model_copy`, not to validation.
    return StatementOut(
        id=row.id,
        source=row.source,
        format=row.format,
        file_id=row.file_id,
        statement_date=row.statement_date,
        period_from=row.period_from,
        period_to=row.period_to,
        column_map=row.column_map,
        status=row.status,
        stats=row.stats,
        error_report=row.error_report,
        created_at=row.created_at,
        lines=[StatementLineOut.model_validate(line) for line in lines],
        lines_total=total,
    )


# --- Task 5: the discrepancy register ----------------------------------------


@router.get("/reconciliations", response_model=Page[ReconciliationOut])
async def list_reconciliations(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PAYMENTS_VIEW))],
    status: Annotated[str, Query(pattern=_STATUS_PATTERN)] = RECONCILIATION_STATUSES[0],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=PAGING_MAX)] = 0,
) -> Any:
    """The accountant's own worklist: every `open` row by default, oldest
    first — `ix_reconciliations_open`'s own query — or `?status=resolved` for
    what has already been closed. A row is either a per-line comparison
    (`statement_line_id` set) or one statement-wide provider-settlement
    period row (`statement_line_id` and `transaction_id` both `None`, both
    totals named in `comment` — `statement_service._period_reconciliation`).

    This register answers ONE question: did the money arrive against an
    invoice. It never answers whether a configured receiver's own wallet,
    or the leshoz's own remainder, actually reached its account — a
    `payment_recipients` row is a Payme WALLET, never a bank account, so
    every receiver row's `allocations.account` is structurally `NULL`, and
    the leshoz's own account may be missing too (`tz/12` #15); `matcher.py`'s
    module docstring gives the same limitation for the matching side, and
    this is the same fact seen from the register a human actually reads.
    """
    rows, total = await backoffice_service.list_reconciliations(
        db, status=status, limit=limit, offset=offset, actor=actor
    )
    return Page[ReconciliationOut](
        items=[ReconciliationOut.model_validate(row) for row in rows],
        total=total,
        page=offset // limit + 1,
        page_size=limit,
    )


@router.get("/reconciliations/export.xlsx")
async def export_reconciliations_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PAYMENTS_VIEW))],
    lang: xlsx.Lang = "uz_latn",
    status: Annotated[str, Query(pattern=_STATUS_PATTERN)] = RECONCILIATION_STATUSES[0],
) -> Response:
    """`GET /payments/reconciliations` as a spreadsheet (stage 13, ruling
    #204): the same `payments.view` gate, the same default (`open`) and
    `?status=` filter, every matching row up to the configured cap."""
    items, total, cap = await export.reconciliation_rows(db, actor=actor, lang=lang, status=status)
    filename = f"nomuvofiqliklar-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render_reconciliations(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.post(
    "/reconciliations/{reconciliation_id}/resolve",
    response_model=ReconciliationOut,
)
async def resolve_reconciliation(
    reconciliation_id: uuid.UUID,
    body: ReconciliationResolveIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PAYMENTS_MANAGE))],
) -> Any:
    """Close one register row — `tz/08`: with a comment, or with a
    correcting document (`resolution_doc_id`, which must name an ACTIVE
    `media_files` row). This is the accountant's own action, never an
    automatic one: nothing in this module ever resolves a row on its own,
    which is also why an open row with `assigned_to` set needs no separate
    task table (this file's own module docstring).

    An empty/whitespace-only comment is `ERR-VAL-001`; a row already
    `resolved` is `ERR-PAY-005` (409) rather than a silent second closure
    that would overwrite the first accountant's own comment.
    """
    row = await backoffice_service.resolve_reconciliation(
        db,
        reconciliation_id,
        comment=body.comment,
        resolution_doc_id=body.resolution_doc_id,
        actor=actor,
    )
    return ReconciliationOut.model_validate(row)


# --- Tasks 6-7: the maker-checker manual PAID --------------------------------
#
# `tz/08` §4's ONE exception to `tz/05` invariant 3, split across two
# permissions on purpose: the accountant (`payments.manage`) FILES, the
# leshoz head (`payments.confirm`, granted to `executor_head` by migration
# 0022) DECIDES. One person can never do both — `backoffice_service.
# check_manual_confirmation` refuses `actor.id == maker_id` before any write,
# including for `sys_admin`, whom `require_permission` lets past the code as
# a superuser: whose two pairs of eyes saw this money is not a permission
# question.
#
# **No `Idempotency-Key` on any of the three.** A replayed filing is refused
# by the "one `pending_check` at a time" guard (`ERR-PAY-004`), and a
# replayed decision by the status check plus
# `uq_provider_transactions_external` — the same reasoning
# `permits.service.issue` gives for answering `ERR-PERM-001` instead of
# minting a marker row. The routes that DO need one are those whose replay
# would create a second row nothing refuses (`POST /payments/bank-statements`
# above, `POST /invoices/{id}/pay-intents`).


@router.get("/manual-confirmations", response_model=Page[ManualConfirmationOut])
async def list_manual_confirmations(
    db: Annotated[AsyncSession, Depends(get_db)],
    # Both roles that can act on a filing need to find it: the maker tracking
    # their own submission (`payments.manage`) and the checker whose worklist
    # this IS (`payments.confirm`) — task defect 4b, the maker previously
    # handed the invoice id to the checker by hand.
    actor: Annotated[User, Depends(require_any_permission(PAYMENTS_MANAGE, PAYMENTS_CONFIRM))],
    status: Annotated[
        str, Query(pattern=_MANUAL_CONFIRMATION_STATUS_PATTERN)
    ] = MANUAL_CONFIRMATION_STATUSES[0],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=PAGING_MAX)] = 0,
) -> Any:
    """The checker's own worklist: every `pending_check` filing by default,
    oldest first — mirrors `list_reconciliations`'s open-by-default shape —
    or `?status=confirmed`/`?status=rejected` for what has already been
    decided. Zone-scoped like every list in this system (fails closed);
    `backoffice_service.list_manual_confirmations`'s own docstring explains
    why that happens per row rather than in this query."""
    rows, total = await backoffice_service.list_manual_confirmations(
        db, status=status, limit=limit, offset=offset, actor=actor
    )
    return Page[ManualConfirmationOut](
        items=[ManualConfirmationOut.model_validate(row) for row in rows],
        total=total,
        page=offset // limit + 1,
        page_size=limit,
    )


@router.get("/manual-confirmations/export.xlsx")
async def export_manual_confirmations_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(PAYMENTS_MANAGE, PAYMENTS_CONFIRM))],
    lang: xlsx.Lang = "uz_latn",
    status: Annotated[
        str, Query(pattern=_MANUAL_CONFIRMATION_STATUS_PATTERN)
    ] = MANUAL_CONFIRMATION_STATUSES[0],
) -> Response:
    """`GET /payments/manual-confirmations` as a spreadsheet (stage 13,
    ruling #204): the same maker-or-checker gate, the same default
    (`pending_check`) and `?status=` filter, every matching row up to the
    configured cap."""
    items, total, cap = await export.manual_confirmation_rows(
        db, actor=actor, lang=lang, status=status
    )
    filename = f"qolda-tasdiqlar-{business_today().isoformat()}.xlsx"
    rendered = export.render_manual_confirmations(items, lang=lang)
    return xlsx.xlsx_response(rendered, filename=filename, total=total, cap=cap)


@router.post("/manual-confirmations", status_code=201, response_model=FiledManualConfirmationOut)
async def file_manual_confirmation(
    body: ManualConfirmationIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PAYMENTS_MANAGE))],
) -> Any:
    """The MAKER's half: an accountant files that money arrived by bank
    transfer, with the payment order behind it.

    **201 means filed, not paid** (ruling 4). The invoice is untouched, no
    ledger row is written and no risk indicator is raised — RI-01 fires when
    the invoice actually becomes PAID, which is the checker's step below.

    `amount_matches_invoice: false` on the response means the bank document's
    amount disagrees with the invoice's; the filing is still accepted (ruling
    5 — an underpayment is a real thing an accountant confirms and then
    reconciles) and an OPEN `reconciliations` row now carries the difference
    in the same register `GET /payments/reconciliations` serves.
    """
    filed = await backoffice_service.file_manual_confirmation(
        db,
        invoice_id=body.invoice_id,
        amount=body.amount,
        paid_at=body.paid_at,
        bank_doc_file_id=body.bank_doc_file_id,
        actor=actor,
    )
    return FiledManualConfirmationOut(
        **ManualConfirmationOut.model_validate(filed.confirmation).model_dump(),
        amount_matches_invoice=filed.amount_matches_invoice,
    )


@router.post(
    "/manual-confirmations/{confirmation_id}/confirm", response_model=ManualConfirmationOut
)
async def confirm_manual_confirmation(
    confirmation_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PAYMENTS_CONFIRM))],
) -> Any:
    """The CHECKER's approval — the only place in this system where an
    invoice becomes `paid` without a payment provider saying so.

    It pays through `payments.service.confirm_payment`, unchanged: a
    synthetic `provider="manual"` transaction goes into the very function the
    Payme webhook calls, so the ledger, the application's move to PAID, the
    applicant's notification and the `payment_confirmed` bus hop all happen
    exactly once and exactly the same way (ruling 14). RI-01 is written to
    the audit journal as a `result="success"` row.

    `ERR-ACL-001` if the caller filed this confirmation; `ERR-PAY-004` if it
    was already decided, or if the invoice stopped being `pending` while it
    waited (a Payme payment may have landed meanwhile).
    """
    row = await backoffice_service.check_manual_confirmation(
        db, confirmation_id, approve=True, reason=None, actor=actor
    )
    return ManualConfirmationOut.model_validate(row)


@router.post("/manual-confirmations/{confirmation_id}/reject", response_model=ManualConfirmationOut)
async def reject_manual_confirmation(
    confirmation_id: uuid.UUID,
    body: ManualConfirmationRejectIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PAYMENTS_CONFIRM))],
) -> Any:
    """The CHECKER's refusal (ruling 7): the invoice stays `pending`, no
    allocation and no synthetic transaction are written, and no RI-01 is
    raised — nothing became PAID.

    A `reason` is mandatory and must not be blank (`ERR-VAL-001`): a
    rejection is what the accountant reads to file a corrected one, and the
    invoice is free to receive a fresh filing afterwards.
    """
    row = await backoffice_service.check_manual_confirmation(
        db, confirmation_id, approve=False, reason=body.reason, actor=actor
    )
    return ManualConfirmationOut.model_validate(row)


# --- Task 10: the ledger read route -------------------------------------------


@router.get("/allocations", response_model=Page[AllocationOut])
async def list_allocations(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_permission(PAYMENTS_VIEW))],
    invoice_id: Annotated[uuid.UUID | None, Query()] = None,
    period_from: Annotated[date | None, Query()] = None,
    period_to: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=PAGING_MAX)] = 0,
) -> Any:
    """The whole `allocations` ledger, oldest first — `payment`, `correction`
    (a reversal's negation, `service.record_reversal`) and `refund` (a
    returned refund's negative entries, `backoffice_service.approve_refund`)
    rows alike, never filtered by `entry_type`. Selected either by ONE
    invoice (`?invoice_id=`) or by an `occurred_at` PERIOD
    (`?period_from=&period_to=`, a calendar-day pair in Asia/Tashkent);
    neither given, or only one half of the pair, is `ERR-VAL-001` — a route
    with no filter at all would page the whole ledger this system will ever
    write, and a half-given pair silently hides the rows a reversed or
    incomplete range would miss (the lesson on a reversed date period).

    **This route answers "did the money arrive against an invoice" — never
    "did a configured receiver's own wallet, or the leshoz's own
    remainder, actually reach its account"** (ruling 10, the same
    limitation `GET /payments/reconciliations`'s own docstring states):
    `account` is `null`, present on EVERY row, whenever that row names a
    configured receiver (STRUCTURALLY — a `payment_recipients` row is a
    Payme wallet, never a bank account, the seeded state-budget row
    included) or names a leshoz with no account on file (`tz/12` #15). A
    client renders that `null` as "settled outside the system", never as a
    blank account number. `recipient_id`/`recipient_name` (task 8) name
    WHICH configured receiver a `target="receiver"` row belongs to.
    """
    rows, total = await backoffice_service.list_allocations(
        db,
        invoice_id=invoice_id,
        period_from=period_from,
        period_to=period_to,
        limit=limit,
        offset=offset,
    )
    return Page[AllocationOut](
        items=await backoffice_service.allocations_out(db, rows),
        total=total,
        page=offset // limit + 1,
        page_size=limit,
    )
