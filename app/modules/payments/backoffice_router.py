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
"did each half of the 50/50 split reach its own account", because the state
budget's account number is stored nowhere in this system (`tz/12` #15) and a
leshoz's own `requisites` may legitimately carry no `"account"` key — so at
least half of every `allocations` row has `account = NULL`. `matcher.py`'s
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

from fastapi import APIRouter, Depends, Form, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files, settings_store
from app.core.deps import get_db
from app.core.errors import err
from app.core.idempotency import IdempotencyContext
from app.core.schemas import PAGING_MAX, Page
from app.modules.auth.deps import idempotency_context, require_permission
from app.modules.auth.models import User
from app.modules.payments import backoffice_service, statement_service
from app.modules.payments.backoffice_schemas import (
    ReconciliationOut,
    ReconciliationResolveIn,
    StatementAccepted,
    StatementLineOut,
    StatementOut,
)
from app.modules.payments.models import RECONCILIATION_STATUSES
from app.modules.payments.permissions import PAYMENTS_MANAGE, PAYMENTS_VIEW
from app.modules.payments.statement_parser import OPTIONAL_FIELDS, REQUIRED_FIELDS

router = APIRouter(prefix="/payments", tags=["payments"])

_KNOWN_FIELDS = frozenset(REQUIRED_FIELDS) | frozenset(OPTIONAL_FIELDS)
# Derived from the model's own tuple, never retyped: a status added to
# `RECONCILIATION_STATUSES` widens what this query parameter accepts with no
# second edit required here.
_STATUS_PATTERN = "^(" + "|".join(RECONCILIATION_STATUSES) + ")$"


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
    invoice. It never answers whether each half of the 50/50 split reached
    its own account — the budget's account number is in no table at all
    (`tz/12` #15), so at least half of every `allocations` row has
    `account = NULL`; `matcher.py`'s module docstring gives the same
    limitation for the matching side, and this is the same fact seen from
    the register a human actually reads.
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
