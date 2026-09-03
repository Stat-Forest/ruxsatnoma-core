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

# How many bad rows `error_report` STORES. It is one JSONB column returned whole
# by `GET /payments/bank-statements/{id}`, so unbounded it is an unbounded column
# and a response that cannot be serialized — and under a 10 MB upload cap a
# statement exported with the wrong column map reaches that by accident, every
# row failing identically. This is a bound on the STORED report only: `parse_csv`
# accumulates its `LineError`s without a cap, and capping that is the parser's own
# business, not this task's. Same value and marker shape as
# `gis.importer.MAX_REPORT_ROWS`, defined here rather than imported: `payments`
# does not reach into `gis`.
MAX_REPORT_ROWS = 200

# How far from a line's `operation_date` an invoice may have been ISSUED and still
# be offered as a hint for an unmatched payment (ruling 11). An invoice's own
# payment window is ten days (`payments.jobs.expiry_sweep`), and a bank posts with
# a day or two of lag, so a fortnight covers a real late payment without turning
# the hint into "every invoice for that amount, ever".
CANDIDATE_WINDOW_DAYS = 14

# At most this many candidates are named in one `reconciliations.comment`. The
# hint exists to give an accountant somewhere to start, not to reproduce a query
# result inside a text column.
MAX_CANDIDATES = 5


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
    """The stored, accountant-visible shape of the parser's own errors.

    Bounded by `MAX_REPORT_ROWS` and stating the TRUE number omitted — a report
    that under-states its own truncation is worse than one that does not
    truncate. The bound is on what is STORED and returned, not on what the
    parser accumulated to get here."""
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


def _is_provider_settlement(line: ParsedLine, provider: str) -> bool:
    """A case-insensitive fragment check of the line's payer name against
    `provider_settlement_payer_fragment` (default `"payme"`).

    Provider money lands in our own cashbox wallet and reaches a leshoz later as
    ONE aggregated payout, so such a line stands for many invoices at once and
    must never be matched to one — nor dumped into the exception register, which
    would bury the whole month's provider turnover under it (ruling 10).

    **The setting names the PROVIDER, and is matched as a fragment of the payer
    name.** One value drives both halves of the settlement comparison: the line
    is recognised by it here, and `_period_reconciliation` asks for that same
    provider's turnover. They were two independent values until a review pointed
    out that retuning the fragment silently left the turnover side comparing
    against `payme` — a settlement check that answers about the wrong provider is
    worse than one that answers nothing. It works as one value because the
    provider code is what a bank writes inside the payer name («PAYME TRANSIT»),
    so an exact match on `provider_transactions.provider` and a substring match
    on `payer_name` are satisfied by the same string.
    """
    if not provider or line.payer_name is None:
        return False
    return provider.casefold() in line.payer_name.casefold()


async def _candidate_hint(db: AsyncSession, line: ParsedLine) -> str:
    """Ruling 11's HINT for a payment whose purpose names no invoice we hold:
    the invoices that agree with this line on AMOUNT and fall near its date.

    **A hint is never a match.** It is written into `reconciliations.comment` and
    nowhere else — it does not set `matched_invoice_id`, does not change
    `match_status`, and no code path can promote it. That is ruling 11's whole
    point: two leshozes can bill the same sum on the same day, so amount and date
    can only ever suggest, and the accountant is the one who decides. Saying "no
    candidates" out loud is part of it — an empty comment would read as "nobody
    looked".
    """
    candidates = await repo.list_invoice_candidates(
        db,
        amount=line.amount,
        since=line.operation_date - timedelta(days=CANDIDATE_WINDOW_DAYS),
        until=line.operation_date + timedelta(days=1),
        limit=MAX_CANDIDATES + 1,
    )
    if not candidates:
        return "no invoice number in the purpose; no candidates by amount and date"
    named = ", ".join(
        f"{invoice.number} (issued {invoice.issued_at.date().isoformat()})"
        for invoice in candidates[:MAX_CANDIDATES]
    )
    more = " and more" if len(candidates) > MAX_CANDIDATES else ""
    return (
        f"no invoice number in the purpose; candidates by amount and date: "
        f"{named}{more} — a hint for the accountant, never an automatic match"
    )


async def _period_reconciliation(
    db: AsyncSession, row: BankStatement, settlements: list[ParsedLine], *, provider: str
) -> list[Reconciliation]:
    """ONE reconciliation row for the whole statement, comparing its provider
    payouts against that provider's turnover over the statement's covered range.

    **Per-invoice matching of provider money is impossible by design**: a payout
    is one aggregated settlement standing for many invoices, and nothing in it
    names any of them. The only honest comparison is a total against a total —
    which is why this reads `repo.list_provider_transactions_in_period`, the
    function 3.10a already wrote for Payme's own `GetStatement`, rather than
    growing a second query that would drift from it.

    **The window is the whole statement, not one day.** It was per-day until a
    review pointed out what that costs: a provider settles with a LAG, so
    yesterday's transactions arrive in today's payout and a per-day comparison
    opens a discrepancy on essentially every payout day — the register-flooding
    this stage exists to prevent, merely moved from per-line rows to period rows.
    A statement-wide window nets the lag out inside itself and leaves only the
    effect at the two boundaries. No lag setting is invented for it: nobody can
    source a default, and a wrong one would be worse than the boundary effect.

    Only PERFORMED transactions count — money a cancelled transaction never moved
    is not missing from the payout, and counting it would manufacture a
    discrepancy out of every cancellation. `payme.STATE_PERFORMED` is that
    provider's own vocabulary and Payme is the only writer of
    `provider_transactions` today; a second provider makes this filter
    provider-specific, which is the moment to split it.

    Returns a list (empty when the statement carries no payout at all) so the
    caller can concatenate it with the per-line rows unconditionally.
    """
    if not settlements:
        return []
    period_from = min(parsed.operation_date for parsed in settlements)
    period_to = max(parsed.operation_date for parsed in settlements)
    if row.period_from is not None and row.period_to is not None:
        period_from, period_to = row.period_from, row.period_to
    paid_out = sum((parsed.amount for parsed in settlements), Decimal("0.00"))
    since, _ = day_bounds(period_from)
    _, until = day_bounds(period_to)
    turnover = sum(
        (
            transaction.amount
            for transaction, _number in await repo.list_provider_transactions_in_period(
                db, provider, since, until
            )
            if transaction.state == payme.STATE_PERFORMED
        ),
        Decimal("0.00"),
    )
    difference = paid_out - turnover
    agrees = difference == 0
    return [
        Reconciliation(
            result="matched" if agrees else "discrepancy",
            difference=None if agrees else difference,
            status="resolved" if agrees else "open",
            # The row belongs to no single line and `reconciliations` has no
            # `statement_id` column, so the statement is named here — which is
            # also how the accountant knows which file to open.
            comment=(
                f"{provider} settlement for "
                f"{period_from.isoformat()}..{period_to.isoformat()}: "
                f"payout {paid_out} vs provider turnover {turnover} "
                f"(statement {row.id})"
            ),
        )
    ]


def _build_rows(
    row: BankStatement,
    lines: list[ParsedLine],
    outcomes: list[matcher.MatchOutcome],
    invoices: list[Invoice | None],
    comments: list[str | None],
) -> tuple[list[BankStatementLine], list[tuple[int, Reconciliation]], dict[str, int]]:
    """Turn one parsed statement into the rows it becomes. Pure — no session —
    so what is written is decided in one readable pass and the savepoint below
    only performs it.

    Returns the line rows, the reconciliation rows paired with the INDEX of the
    line each belongs to (their `statement_line_id` can only be filled in once
    the lines have been flushed and have ids), and the per-status counters
    `stats` reports. `comments` is the caller's per-line hint (ruling 11's
    amount+date candidates), positional like every other list here — the lookup
    it needs is a query, which is why it arrives ready-made rather than being
    done in this pure function.
    """
    line_rows: list[BankStatementLine] = []
    pending: list[tuple[int, Reconciliation]] = []
    counts: dict[str, int] = {}
    for index, (parsed, outcome, invoice, hint) in enumerate(
        zip(lines, outcomes, invoices, comments, strict=True)
    ):
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
                    # The matcher never sets a comment today; the amount+date
                    # hint of ruling 11 is the service's own, because finding
                    # candidates needs a query and the matcher is pure.
                    comment=hint or outcome.comment,
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


async def _import(
    db: AsyncSession, row: BankStatement, errors: list[LineError]
) -> tuple[str, dict[str, Any] | None]:
    """Load, parse, match and WRITE one statement, extending `errors` with
    everything the file got wrong. Returns the status and the `stats` to stamp;
    `run_statement` is the only caller and the only place `_finish` is called.

    Raises on anything that is not "a bad file" — a `csv.Error`, a row the
    database refuses, a bug of ours. `run_statement` turns that into the same
    `failed` record, which is the point: nothing may escape to the worker and
    leave the row `pending` to be re-claimed every thirty seconds forever.
    """
    data = await _load_file(db, row)
    if data is None:
        errors.append(
            LineError(line_no=1, field="file", message="the stored statement file is gone")
        )
        return "failed", None

    lines, parse_errors = statement_parser.parse_csv(data, column_map=row.column_map or {})
    errors.extend(parse_errors)
    if not lines:
        # Nothing to import. Either the parser short-circuited the whole file
        # (undecodable bytes, a required column the header does not have) or
        # every row was bad — in both cases there is no partial success to keep.
        return "failed", None

    provider = await settings_store.get_str(db, "provider_settlement_payer_fragment")
    # The savepoint of the atomicity rule: everything below is undone together,
    # while the outer transaction — and the failure record `run_statement` writes
    # on it — survives.
    async with db.begin_nested():
        cache: dict[str, Invoice | None] = {}
        outcomes: list[matcher.MatchOutcome] = []
        invoices: list[Invoice | None] = []
        comments: list[str | None] = []
        for parsed in lines:
            settlement = _is_provider_settlement(parsed, provider)
            if settlement:
                number, invoice = None, None
            else:
                number, invoice = await _invoice_for(db, parsed, cache)
            outcome = matcher.classify(
                parsed,
                invoice_amount=invoice.amount if invoice is not None else None,
                invoice_found=number is not None and invoice is not None,
                is_provider_settlement=settlement,
            )
            outcomes.append(outcome)
            invoices.append(invoice)
            comments.append(
                await _candidate_hint(db, parsed)
                if outcome.match_status == "unknown_payment"
                else None
            )

        # The covered range is stamped BEFORE the period row is built: that row
        # compares the statement's whole window, and reading it off the same
        # columns the accountant sees keeps the two from disagreeing.
        row.period_from = min(parsed.operation_date for parsed in lines)
        row.period_to = max(parsed.operation_date for parsed in lines)

        line_rows, pending, counts = _build_rows(row, lines, outcomes, invoices, comments)
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
            + await _period_reconciliation(db, row, settlements, provider=provider),
        )
    return "parsed", {"imported": len(lines), "skipped": len(parse_errors), **counts}


async def run_statement(db: AsyncSession, row: BankStatement) -> None:
    """Process ONE claimed statement, in the caller's transaction.

    **Never raises.** Every failure a delivery can cause becomes
    `status="failed"` plus an `error_report`, and so does every failure our own
    code can cause: the guard wraps the WHOLE import, not just the writes.

    That distinction was a real bug. The file load and the parse used to sit
    outside it on the argument that only the writes can fail — but the stdlib
    `csv` module raises `Error: field larger than field limit (131072)` on an
    unclosed quote in a file over 128 KB, which is a FILE's doing, not ours. The
    exception escaped, the transaction rolled back, and the statement stayed
    `pending` for the scheduler to re-claim every thirty seconds, forever. A
    failure that cannot be recorded is a failure that never stops.

    The outer transaction is still usable in the handler: writes only ever
    happen inside `_import`'s savepoint, which is rolled back on the way out,
    and everything before it touches nothing.

    `statement_id` is read ONCE, before the try. Rolling the savepoint back
    EXPIRES every instance modified inside it — `row` among them — and reading
    an expired attribute is a lazy load, which in async SQLAlchemy is IO
    attempted from a plain attribute access: `MissingGreenlet`, raised from the
    handler, replacing the recorded failure with an unrecorded one. `_finish`
    below is safe for the same reason in reverse: it only ASSIGNS until its
    `flush()`, and reads `row.id` after it, inside the greenlet context.
    """
    statement_id = row.id
    row.status = "parsing"
    await db.flush()
    errors: list[LineError] = []
    try:
        status, stats = await _import(db, row, errors)
    except Exception:
        logger.exception("payments.statement.import_failed", statement_id=str(statement_id))
        status, stats = "failed", None
        errors.append(
            LineError(
                line_no=1,
                field="import",
                message="the statement could not be imported — check the file and re-upload it",
            )
        )
    await _finish(db, row, status=status, stats=stats, errors=errors)


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
