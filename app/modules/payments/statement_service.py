"""The bank statement's own state machine: claim -> parse -> match -> report
(plan `03.10b-payments-reconciliation` task 4).

Importing a statement is a JOB, not a request. `POST /payments/bank-statements`
only stores the file and answers 202; everything below runs later, in a worker,
so a month's statement does not need the accountant's browser to stay open. It
is deliberately not the outbox — the outbox carries messages LEAVING the
system, this is inbound work. The whole shape is `gis/import_service.py`'s,
including its two rules:

* **The batch is atomic.** All of one statement's row writes live in ONE
  savepoint: if anything refuses them, every one is rolled back while the
  failure record itself — `status="failed"` plus the `error_report` the
  accountant needs in order to fix the file — is written on the OUTER
  transaction and survives.
* **Warnings are not errors.** One unparseable row out of three hundred is a
  line in `error_report` and a `skipped` count in `stats`; the other 299 are
  imported and the statement is `parsed`. Only a file that yielded NOTHING is
  `failed`.

**Matching a bank line does not pay an invoice.** `tz/05` invariant 3: an
invoice becomes `paid` only on a provider's confirmation or through the
maker-checker manual path (ruling 14) — both of which go through
`payments.service.confirm_payment`. Everything here OBSERVES: it writes
`bank_statement_lines` and `reconciliations` rows and touches `invoices` not
at all. A `matched` line is a closed observation (`result="matched"`,
`status="resolved"`); anything else is an open row in the accountant's
register.

**The account is never a matching key** — the reason is in `matcher.py`'s own
module docstring and is not re-derived here. `payer_account` is stored raw on
the line for the accountant to read and decides nothing.
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, storage
from app.core.errors import err
from app.core.models import MediaFile
from app.core.time import TASHKENT
from app.modules.audit import service as audit
from app.modules.payments import matcher, payme, repo, statement_parser
from app.modules.payments.models import (
    BankStatement,
    BankStatementLine,
    Invoice,
    Reconciliation,
)
from app.modules.payments.statement_parser import LineError, ParsedLine

logger = structlog.get_logger(__name__)

CREATE_ACTION = "bank_statement.create"
FINISH_ACTION = "bank_statement.finish"

# This module's OWN upload table, not `core.files.ALLOWED_TYPES` (ruling 8's
# shape, borrowed from gis): a bank statement is not a document, and routing it
# through `POST /files` would mean widening that whitelist for every uploader in
# the system. The prefix tuple is empty because a CSV has no magic — it starts
# with whatever its first column header happens to be — so the real gate is the
# parser, which `core.files._magic_ok` documents as the meaning of an empty tuple.
STATEMENT_UPLOAD_TYPES: dict[str, tuple[bytes, ...]] = {"text/csv": ()}

# `error_report` is one JSONB column returned whole by
# `GET /payments/bank-statements/{id}`. Unbounded it is unbounded worker memory,
# an unbounded column and a response that cannot be serialized — and under a
# 10 MB cap a statement exported with the wrong column map reaches that by
# accident, every row failing identically. Same cap and marker shape as
# `gis.importer.MAX_REPORT_ROWS`, defined here rather than imported: `payments`
# does not reach into `gis`.
MAX_REPORT_ROWS = 200


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """The UTC instants one Asia/Tashkent calendar day spans.

    A statement's `operation_date` is a calendar day written by a bank in
    Tashkent; `provider_transactions.received_at` is a UTC instant. Comparing
    the two by naive UTC midnight would shift every period by five hours and
    put the evening's payments in the wrong day (the same trap
    `core.time.business_today` exists for)."""
    start = datetime.combine(day, time.min, tzinfo=TASHKENT).astimezone(UTC)
    return start, start + timedelta(days=1) - timedelta(microseconds=1)


def _error_report(errors: list[LineError]) -> dict[str, Any] | None:
    """The stored, accountant-visible shape of the parser's own errors. Bounded
    (see `MAX_REPORT_ROWS`) and stating the TRUE number omitted, never one
    producer's count — a report that under-states its own truncation is worse
    than one that does not truncate."""
    if not errors:
        return None
    omitted = max(len(errors) - MAX_REPORT_ROWS, 0)
    report: dict[str, Any] = {
        "errors": [
            {"line_no": e.line_no, "field": e.field, "message": e.message}
            for e in errors[:MAX_REPORT_ROWS]
        ]
    }
    if omitted:
        report["omitted"] = omitted
    return report


async def _load_file(db: AsyncSession, row: BankStatement) -> bytes | None:
    """The uploaded bytes back out of MinIO. `None` means the object is gone (a
    wiped bucket, a lifecycle rule): the statement is unrunnable, which is a
    `failed` record — not an exception that would make the job re-claim the same
    row every ten seconds, forever."""
    if row.file_id is None:  # a future `source="api"` feed carries no file at all
        return None
    file = await db.get(MediaFile, row.file_id)
    if file is None:
        return None
    try:
        return await storage.get_object(file.storage_key)
    except FileNotFoundError:
        return None


async def _invoice_for(
    db: AsyncSession, line: ParsedLine, cache: dict[str, Invoice | None]
) -> tuple[str | None, Invoice | None]:
    """The invoice the line's `purpose` names, if any. Cached per statement: a
    month's statement can carry the same invoice number on several lines (a
    part payment and its remainder), and one SELECT per line would be one
    SELECT per line for nothing."""
    number = matcher.extract_invoice_number(line.purpose)
    if number is None:
        return None, None
    if number not in cache:
        cache[number] = await repo.get_invoice_by_number(db, number)
    return number, cache[number]


def _is_provider_settlement(line: ParsedLine, fragment: str) -> bool:
    """A case-insensitive fragment check of the line's payer name against
    `provider_settlement_payer_fragment` (default `"payme"`). Payme money lands
    in our own cashbox wallet and reaches a leshoz later as ONE aggregated
    payout, so such a line stands for many invoices at once and must never be
    matched to one — nor dumped into the exception register, which would bury
    the whole month's provider turnover under it (ruling 10)."""
    if not fragment or line.payer_name is None:
        return False
    return fragment.casefold() in line.payer_name.casefold()


async def _period_reconciliations(
    db: AsyncSession, row: BankStatement, settlements: list[ParsedLine]
) -> list[Reconciliation]:
    """One reconciliation row per period the statement's provider payouts
    cover, comparing the payout total against the provider's own turnover for
    the same period.

    **Per-invoice matching of provider money is impossible by design**: a Payme
    payout is one aggregated settlement standing for many invoices, and nothing
    in it names any of them. The only honest comparison is a total against a
    total — which is why this reads `repo.list_provider_transactions_in_period`,
    the function 3.10a already wrote for Payme's own `GetStatement`, rather than
    growing a second query that would drift from it.

    The period is one calendar day: that is the finest grain a statement line
    carries (`operation_date` is a date, not an instant), and a day that
    disagrees is a day an accountant can actually go and look at. Only
    PERFORMED transactions count — money a cancelled transaction never moved is
    not missing from the payout, and counting it would manufacture a
    discrepancy out of every cancellation.
    """
    rows: list[Reconciliation] = []
    for day in sorted({line.operation_date for line in settlements}):
        paid_out = sum(
            (line.amount for line in settlements if line.operation_date == day), Decimal("0.00")
        )
        since, until = day_bounds(day)
        turnover = sum(
            (
                transaction.amount
                for transaction, _number in await repo.list_provider_transactions_in_period(
                    db, payme.PROVIDER, since, until
                )
                if transaction.state == payme.STATE_PERFORMED
            ),
            Decimal("0.00"),
        )
        difference = paid_out - turnover
        agrees = difference == 0
        rows.append(
            Reconciliation(
                result="matched" if agrees else "discrepancy",
                difference=None if agrees else difference,
                status="resolved" if agrees else "open",
                comment=(
                    f"{payme.PROVIDER} settlement for {day.isoformat()}: "
                    f"payout {paid_out} vs provider turnover {turnover} "
                    f"(statement {row.id})"
                ),
            )
        )
    return rows


def _build_rows(
    row: BankStatement,
    lines: list[ParsedLine],
    outcomes: list[matcher.MatchOutcome],
    invoices: list[Invoice | None],
) -> tuple[list[BankStatementLine], list[tuple[int, Reconciliation]], dict[str, int]]:
    """Turn one parsed statement into the rows it becomes. Pure — no session —
    so what is written is decided in one readable pass and the savepoint below
    only performs it.

    Returns the line rows, the reconciliation rows paired with the INDEX of the
    line each belongs to (their `statement_line_id` can only be filled in once
    the lines have been flushed and have ids), and the per-status counters
    `stats` reports.
    """
    line_rows: list[BankStatementLine] = []
    pending: list[tuple[int, Reconciliation]] = []
    counts: dict[str, int] = {}
    for index, (parsed, outcome, invoice) in enumerate(zip(lines, outcomes, invoices, strict=True)):
        counts[outcome.match_status] = counts.get(outcome.match_status, 0) + 1
        line_rows.append(
            BankStatementLine(
                statement_id=row.id,
                line_no=parsed.line_no,
                doc_number=parsed.doc_number,
                amount=parsed.amount,
                operation_date=parsed.operation_date,
                payer_name=parsed.payer_name,
                payer_account=parsed.payer_account,
                purpose=parsed.purpose,
                raw=parsed.raw,
                match_status=outcome.match_status,
                matched_invoice_id=invoice.id if invoice is not None else None,
            )
        )
        # A provider settlement gets no per-line row at all — it is reconciled
        # as a period total instead (`_period_reconciliations`). Everything else
        # gets one: `matched` closed on the spot, the rest open on the
        # accountant's register.
        if outcome.result is None:
            continue
        resolved = outcome.match_status == "matched"
        pending.append(
            (
                index,
                Reconciliation(
                    invoice_id=invoice.id if invoice is not None else None,
                    result=outcome.result,
                    difference=outcome.difference,
                    status="resolved" if resolved else "open",
                    comment=outcome.comment,
                ),
            )
        )
    return line_rows, pending, counts


async def _finish(
    db: AsyncSession,
    row: BankStatement,
    *,
    status: str,
    stats: dict[str, Any] | None = None,
    errors: list[LineError] | None = None,
) -> None:
    """The one exit of every import, successful or not: stamp the row and audit
    it. The SINGLE write point for both report columns, so the bound on what is
    stored holds regardless of which branch got here.

    A job audits with `user_id=None` and its own correlation id (`CLAUDE.md`'s
    worker idiom). No notification: unlike a geodata import there is no seeded
    `notification_templates` row for a statement, and inventing an event code
    with no template behind it would fail at delivery time rather than here.
    """
    row.status = status
    row.stats = stats or {}
    row.error_report = _error_report(errors or [])
    await db.flush()
    await audit.log(
        db,
        action=FINISH_ACTION,
        user_id=None,
        object_type="bank_statement",
        object_id=row.id,
        correlation_id=f"job:{uuid.uuid4()}",
        result="success" if status == "parsed" else "error",
        new_value={"status": status, **(stats or {}), "errors": len(errors or [])},
    )


async def run_statement(db: AsyncSession, row: BankStatement) -> None:
    """Process ONE claimed statement, in the caller's transaction.

    Never raises for a bad file: every failure a delivery can cause becomes
    `status="failed"` plus an `error_report`, which is the deliverable. Our OWN
    defects are caught too — they happen inside the savepoint, which rolls back
    without poisoning the outer transaction, so a crashed statement is recorded
    as failed rather than re-claimed by every tick of the scheduler forever.
    """
    row.status = "parsing"
    await db.flush()

    data = await _load_file(db, row)
    if data is None:
        await _finish(
            db,
            row,
            status="failed",
            errors=[
                LineError(line_no=1, field="file", message="the stored statement file is gone")
            ],
        )
        return

    lines, errors = statement_parser.parse_csv(data, column_map=row.column_map or {})
    if not lines:
        # Nothing to import. Either the parser short-circuited the whole file
        # (undecodable bytes, a required column the header does not have) or
        # every row was bad — in both cases there is no partial success to keep.
        await _finish(db, row, status="failed", errors=errors)
        return

    fragment = await settings_store.get_str(db, "provider_settlement_payer_fragment")
    try:
        # The savepoint: everything the batch writes is undone together, while
        # the outer transaction — and the failure record written on it below —
        # survives.
        async with db.begin_nested():
            cache: dict[str, Invoice | None] = {}
            outcomes: list[matcher.MatchOutcome] = []
            invoices: list[Invoice | None] = []
            for parsed in lines:
                settlement = _is_provider_settlement(parsed, fragment)
                if settlement:
                    number, invoice = None, None
                else:
                    number, invoice = await _invoice_for(db, parsed, cache)
                outcomes.append(
                    matcher.classify(
                        parsed,
                        invoice_amount=invoice.amount if invoice is not None else None,
                        invoice_found=number is not None and invoice is not None,
                        is_provider_settlement=settlement,
                    )
                )
                invoices.append(invoice)

            line_rows, pending, counts = _build_rows(row, lines, outcomes, invoices)
            await repo.add_statement_lines(db, line_rows)
            for index, reconciliation in pending:
                reconciliation.statement_line_id = line_rows[index].id
            settlements = [
                parsed
                for parsed, outcome in zip(lines, outcomes, strict=True)
                if outcome.match_status == "provider_settlement"
            ]
            await repo.add_reconciliations(
                db,
                [reconciliation for _index, reconciliation in pending]
                + await _period_reconciliations(db, row, settlements),
            )
            row.period_from = min(parsed.operation_date for parsed in lines)
            row.period_to = max(parsed.operation_date for parsed in lines)
    except Exception:
        # Not "a bad file" — reaching here is a defect in our own code or a row
        # the database refused. The savepoint has already undone every write, so
        # the outer transaction is usable and the failure is recordable.
        logger.exception("payments.statement.write_failed", statement_id=str(row.id))
        await _finish(
            db,
            row,
            status="failed",
            errors=[
                *errors,
                LineError(line_no=1, field="import", message="the statement import failed"),
            ],
        )
        return

    await _finish(
        db,
        row,
        status="parsed",
        stats={"imported": len(lines), "skipped": len(errors), **counts},
        errors=errors,
    )


async def process_pending(db: AsyncSession) -> int:
    """Claim and run at most one pending statement, in the caller's session.
    Returns how many were processed — 0 when the queue is empty or every due row
    is already locked by another worker.

    The session-owning half is `app/workers/jobs.py::process_bank_statements`,
    which opens the session and commits — the same split
    `payments.jobs.expiry_sweep` has from `expire_invoices`.
    """
    row = await repo.claim_pending_statement(db)
    if row is None:
        return 0
    await run_statement(db, row)
    return 1


# --- the upload endpoint's own service half ----------------------------------


async def create_statement(
    db: AsyncSession,
    *,
    file_id: uuid.UUID,
    statement_date: date,
    column_map: dict[str, str],
    actor: Any,
) -> BankStatement:
    """`POST /payments/bank-statements` — record the stored file and QUEUE the
    work. The response is 202 with an id; nothing is parsed here.

    `source="file"` is the only value written anywhere: `bank_statements.source`
    also accepts `"api"` for the automatic feed `tz/08` mentions, but ruling 20
    fences that out of this stage and nothing produces it yet.
    """
    row = BankStatement(
        source="file",
        format="csv",
        file_id=file_id,
        statement_date=statement_date,
        column_map=column_map,
        imported_by=actor.id,
        status="pending",
    )
    await repo.add_statement(db, row)
    await audit.log(
        db,
        action=CREATE_ACTION,
        user_id=actor.id,
        object_type="bank_statement",
        object_id=row.id,
        new_value={
            "file_id": str(file_id),
            "statement_date": statement_date.isoformat(),
            "column_map": column_map,
        },
    )
    return row


async def get_statement(
    db: AsyncSession, statement_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[BankStatement, list[BankStatementLine], int]:
    """`GET /payments/bank-statements/{id}` — the statement plus a page of its
    lines. No zone scoping: a bank statement belongs to the accounting
    department, not to a leshoz, and carries no `organization_id` to scope on."""
    row = await repo.get_statement(db, statement_id)
    if row is None:
        raise err("ERR-SYS-003")
    lines, total = await repo.list_statement_lines(db, statement_id, limit=limit, offset=offset)
    return row, lines, total
