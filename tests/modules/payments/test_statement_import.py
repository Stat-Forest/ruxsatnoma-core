"""`POST /payments/bank-statements`, the job that parses it, and the
reconciliation rows an accountant then works from (plan
`03.10b-payments-reconciliation` task 4).

The upload stores and QUEUES — it parses nothing (the same shape
`gis/imports_router.py` has, and for the same reason: a statement is a job,
not a request). What the endpoint must get right is the gate: `payments.manage`,
this module's OWN MIME table (`text/csv`) and cap key, and a mandatory
`Idempotency-Key` so a double-clicked upload never files the same statement
twice.

What the JOB must get right is the whole point of the stage: **matching a bank
line does not pay an invoice** (`tz/05` invariant 3 — only a provider's
confirmation or the maker-checker path does), an unparseable row is a warning
and not a failure, and a Payme payout is reconciled as a period TOTAL rather
than being buried in the exception register invoice by invoice.
"""

import csv
import io
import json
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.modules.payments import payme, statement_service
from app.modules.payments.models import (
    BankStatement,
    BankStatementLine,
    Invoice,
    ProviderTransaction,
    Reconciliation,
)

# The file's own column headers — deliberately Cyrillic and unlike our field
# names, which is the entire reason `column_map` is a per-import parameter
# rather than a guess (statement_parser.py's module docstring).
COLUMN_MAP = {
    "amount": "Сумма",
    "operation_date": "Дата",
    "purpose": "Назначение",
    "payer_name": "Плательщик",
    "payer_account": "Счет",
    "doc_number": "Документ",
}
HEADER = ["Документ", "Дата", "Сумма", "Плательщик", "Счет", "Назначение"]
STATEMENT_DATE = "2026-09-02"
# The settlement test compares a whole DAY's payout against that day's provider
# turnover, so it needs a day no OTHER test can put a `provider_transactions`
# row in. `test_payme_rpc.py` stamps its own rows with the wall clock, so the
# one safe choice is a date the wall clock will not reach.
PAYOUT_DAY = date(2030, 1, 15)


def csv_bytes(*rows: list[str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(HEADER)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def row(
    *,
    doc="1",
    on="02.09.2026",
    amount="2 060 000,00",
    payer="ООО Пастбище",
    account="20208000000000000001",
    purpose="Оплата",
) -> list[str]:
    """One data row. Written through `csv.writer` rather than an f-string
    because the amount a bank exports — «2 060 000,00» — CONTAINS the delimiter
    and has to be quoted; hand-joining it silently produced a seven-column row
    that parsed as garbage."""
    return [doc, on, amount, payer, account, purpose]


def idem() -> dict[str, str]:
    """A FRESH key per call: the client convention is that a retry reuses its
    key and a genuinely new request mints a new one, so a test that is not
    about replay must not accidentally replay (mirrors
    `gis/test_imports_api.py::idem`)."""
    return {"Idempotency-Key": str(uuid.uuid4())}


def form(*, column_map: dict[str, str] | None = None, statement_date: str = STATEMENT_DATE):
    return {
        "statement_date": statement_date,
        "column_map": json.dumps(COLUMN_MAP if column_map is None else column_map),
    }


def upload(client, data: bytes, **kwargs):
    return client.post(
        "/api/v1/payments/bank-statements",
        data=form(**kwargs),
        files={"file": ("vypiska.csv", data, "text/csv")},
        headers=idem(),
    )


@pytest.fixture
async def bank_invoice(db, approved_application) -> Invoice:
    """An invoice whose NUMBER the matcher can actually find in free text —
    `INV-YYYY-NNNNNN` (`matcher.INVOICE_NUMBER_RE`). The module's own `invoice`
    fixture numbers itself with a hex slice, which that regex correctly refuses."""
    row_ = Invoice(
        application_id=approved_application.id,
        number=f"INV-2026-{uuid.uuid4().int % 10**6:06d}",
        amount=Decimal("2060000.00"),
        status="pending",
    )
    db.add(row_)
    await db.flush()
    return row_


async def _statements(db) -> list[BankStatement]:
    return list((await db.execute(select(BankStatement))).scalars().all())


async def drain(db) -> None:
    """Run the queue dry on the test's own session.

    Draining rather than calling `process_pending` once: a statement is created
    over HTTP and is therefore already COMMITTED by the app's own session, while
    this test's writes are rolled back at teardown — so a statement an earlier
    test uploaded is still `pending` in this shared, persistent database, and the
    queue is oldest-first by design. Without the drain a test would silently
    assert against whatever ran before it.

    Deliberately WITHOUT a commit of its own: the loop still terminates (a
    processed row leaves the `status = 'pending'` filter inside this
    transaction), and committing here would make every row a test invents —
    a `provider_transactions` row above all — permanent in a database the next
    run reuses. That is not hypothetical: the settlement test's own turnover
    doubled between two runs before this comment existed.
    """
    while await statement_service.process_pending(db):
        pass


async def _lines(db, statement_id) -> list[BankStatementLine]:
    return list(
        (
            await db.execute(
                select(BankStatementLine)
                .where(BankStatementLine.statement_id == statement_id)
                .order_by(BankStatementLine.line_no)
            )
        )
        .scalars()
        .all()
    )


# --- the upload -------------------------------------------------------------


async def test_an_upload_is_accepted_and_nothing_is_parsed_yet(payments_view_client, db):
    """202 means QUEUED. `payments_view_client` is THE `accountant` role, which
    migration 0017 grants `payments.manage` as well as `payments.view` — so this
    also proves the production role reaches this route."""
    response = await upload(payments_view_client, csv_bytes(row()))
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"

    statement = await db.get(BankStatement, uuid.UUID(body["id"]))
    assert statement is not None
    assert statement.status == "pending"
    assert statement.source == "file" and statement.format == "csv"
    assert statement.file_id is not None  # the bytes are in media_files
    assert statement.column_map == COLUMN_MAP
    assert statement.statement_date == date(2026, 9, 2)
    assert await _lines(db, statement.id) == []  # the endpoint parses nothing


async def test_a_replayed_key_returns_the_first_answer_and_files_no_second_statement(
    payments_view_client, db
):
    key = {"Idempotency-Key": str(uuid.uuid4())}
    data = csv_bytes(row())
    # Ids before and after, not a bare COUNT: the test database is persistent
    # and shared, so every statement any earlier test uploaded is still in it.
    before = {statement.id for statement in await _statements(db)}
    first = await payments_view_client.post(
        "/api/v1/payments/bank-statements",
        data=form(),
        files={"file": ("vypiska.csv", data, "text/csv")},
        headers=key,
    )
    second = await payments_view_client.post(
        "/api/v1/payments/bank-statements",
        data=form(),
        files={"file": ("vypiska.csv", data, "text/csv")},
        headers=key,
    )
    assert first.status_code == 202 and second.status_code == 202, second.text
    assert first.json() == second.json()
    after = {statement.id for statement in await _statements(db)}
    assert len(after - before) == 1


async def test_an_applicant_may_not_upload_a_bank_statement(applicant_client):
    response = await upload(applicant_client, csv_bytes(row()))
    assert response.status_code == 403


async def test_a_pdf_is_not_a_bank_statement(payments_view_client):
    """This module brings its OWN MIME table (`{"text/csv": ()}`), rather than
    widening the document whitelist `POST /files` uses for every uploader."""
    response = await payments_view_client.post(
        "/api/v1/payments/bank-statements",
        data=form(),
        files={"file": ("decree.pdf", b"%PDF-1.4 x", "application/pdf")},
        headers=idem(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "type_not_allowed"


# --- the job ----------------------------------------------------------------


async def test_a_line_naming_its_invoice_for_the_exact_amount_matches_and_pays_nothing(
    payments_view_client, db, bank_invoice
):
    """`tz/05` invariant 3: only a provider's confirmation — or the
    maker-checker path — moves an invoice to `paid`. Reconciliation OBSERVES;
    it never pays."""
    response = await upload(
        payments_view_client,
        csv_bytes(row(purpose=f"Оплата по счёту {bank_invoice.number} от 01.09")),
    )
    statement_id = uuid.UUID(response.json()["id"])

    await drain(db)
    statement = await db.get(BankStatement, statement_id, populate_existing=True)
    assert statement is not None
    assert statement.status == "parsed"

    (line,) = await _lines(db, statement_id)
    assert line.match_status == "matched"
    assert line.matched_invoice_id == bank_invoice.id
    assert line.payer_account == "20208000000000000001"  # stored raw, never a key

    rows = (
        (
            await db.execute(
                select(Reconciliation).where(Reconciliation.statement_line_id == line.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].result == "matched" and rows[0].status == "resolved"
    assert rows[0].invoice_id == bank_invoice.id

    await db.refresh(bank_invoice)
    assert bank_invoice.status == "pending" and bank_invoice.paid_at is None


async def test_a_line_whose_amount_disagrees_opens_a_discrepancy_with_the_signed_difference(
    payments_view_client, db, bank_invoice
):
    """`difference = paid - invoiced`: 2 060 000 invoiced, 2 000 000 arrived,
    so the register carries -60 000 — an underpayment, unambiguously signed."""
    response = await upload(
        payments_view_client,
        csv_bytes(row(amount="2 000 000,00", purpose=f"Оплата по счёту {bank_invoice.number}")),
    )
    statement_id = uuid.UUID(response.json()["id"])
    await drain(db)

    (line,) = await _lines(db, statement_id)
    assert line.match_status == "discrepancy"
    (open_row,) = (
        (
            await db.execute(
                select(Reconciliation).where(Reconciliation.statement_line_id == line.id)
            )
        )
        .scalars()
        .all()
    )
    assert open_row.status == "open"
    assert open_row.result == "discrepancy"
    assert open_row.difference == Decimal("-60000.00")


async def test_a_line_naming_no_invoice_is_an_unknown_payment(payments_view_client, db):
    """Ruling 11: never matched on amount and date alone — two leshozes can
    bill the same sum on the same day."""
    response = await upload(payments_view_client, csv_bytes(row(purpose="Оплата за услуги")))
    statement_id = uuid.UUID(response.json()["id"])
    await drain(db)

    (line,) = await _lines(db, statement_id)
    assert line.match_status == "unknown_payment"
    assert line.matched_invoice_id is None
    (open_row,) = (
        (
            await db.execute(
                select(Reconciliation).where(Reconciliation.statement_line_id == line.id)
            )
        )
        .scalars()
        .all()
    )
    assert open_row.result == "unknown" and open_row.status == "open"


async def test_one_unparseable_row_is_a_warning_and_the_good_rows_still_import(
    payments_view_client, db, bank_invoice
):
    """`gis/import_service.py`'s rule 7, reused: warnings are not errors. A
    single malformed row must not cost the accountant the whole file."""
    response = await upload(
        payments_view_client,
        csv_bytes(
            row(doc="1", purpose=f"Оплата по счёту {bank_invoice.number}"),
            row(doc="2", amount="не сумма"),
            row(doc="3", purpose="Оплата за услуги"),
        ),
    )
    statement_id = uuid.UUID(response.json()["id"])
    await drain(db)

    statement = await db.get(BankStatement, statement_id, populate_existing=True)
    assert statement is not None
    assert statement.status == "parsed"  # NOT failed
    lines = await _lines(db, statement_id)
    assert [line.line_no for line in lines] == [2, 4]  # the header is line 1
    assert statement.stats["imported"] == 2
    assert statement.stats["skipped"] == 1
    errors = (statement.error_report or {})["errors"]
    assert [(e["line_no"], e["field"]) for e in errors] == [(3, "amount")]


async def test_a_column_map_that_names_a_column_the_file_does_not_have_fails_the_file(
    payments_view_client, db
):
    """The parser's own short-circuit: a required column missing from the header
    is one error at line 1 and an empty result — there is nothing to import, so
    this is a `failed` statement, not a `parsed` one with warnings."""
    response = await upload(
        payments_view_client,
        csv_bytes(row()),
        column_map={**COLUMN_MAP, "purpose": "Не такая колонка"},
    )
    statement_id = uuid.UUID(response.json()["id"])
    await drain(db)

    statement = await db.get(BankStatement, statement_id, populate_existing=True)
    assert statement is not None
    assert statement.status == "failed"
    assert (statement.error_report or {})["errors"][0]["field"] == "purpose"
    assert await _lines(db, statement_id) == []  # the savepoint rolled every write back


async def test_a_payme_payout_is_reconciled_as_a_period_total_not_per_invoice(
    payments_view_client, db, bank_invoice
):
    """Ruling 10 + the task's own ruling 4: a provider payout is ONE aggregated
    line standing for many invoices, so it is compared against the period's own
    provider turnover — never matched invoice by invoice, and never dumped into
    the exception register every month."""
    response = await upload(
        payments_view_client,
        csv_bytes(
            row(
                on=PAYOUT_DAY.strftime("%d.%m.%Y"),
                amount="2 000 000,00",
                payer="PAYME TRANSIT",
                purpose="Реестр за 14.01",
            )
        ),
    )
    statement_id = uuid.UUID(response.json()["id"])

    # The provider's own turnover for that day, added AFTER the upload and never
    # followed by another request: `_commit_pending_before_requests` commits this
    # session before every outgoing call, so a transaction staged BEFORE the
    # upload would be committed into a database the next run reuses — and the
    # day's turnover would then double on every run (it did).
    db.add(
        ProviderTransaction(
            invoice_id=bank_invoice.id,
            provider=payme.PROVIDER,
            external_id=uuid.uuid4().hex,
            amount=Decimal("2060000.00"),
            state=payme.STATE_PERFORMED,
            received_at=statement_service.day_bounds(PAYOUT_DAY)[0],
        )
    )
    await db.flush()
    await drain(db)

    (line,) = await _lines(db, statement_id)
    assert line.match_status == "provider_settlement"
    assert (
        await db.scalar(
            select(func.count())
            .select_from(Reconciliation)
            .where(Reconciliation.statement_line_id == line.id)
        )
    ) == 0  # never a per-line exception

    # A period row belongs to no single line, so it names its statement in the
    # comment — which is also how the accountant knows where it came from.
    (period_row,) = (
        (
            await db.execute(
                select(Reconciliation).where(
                    Reconciliation.statement_line_id.is_(None),
                    Reconciliation.comment.contains(str(statement_id)),
                )
            )
        )
        .scalars()
        .all()
    )
    assert period_row.result == "discrepancy" and period_row.status == "open"
    assert period_row.difference == Decimal("-60000.00")  # payout minus provider turnover


# --- the read route ---------------------------------------------------------


async def test_the_status_route_reports_the_statement_and_its_lines(
    payments_view_client, db, bank_invoice
):
    response = await upload(
        payments_view_client, csv_bytes(row(purpose=f"Счёт {bank_invoice.number}"))
    )
    statement_id = response.json()["id"]
    await drain(db)

    read = await payments_view_client.get(f"/api/v1/payments/bank-statements/{statement_id}")
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["status"] == "parsed"
    assert body["lines_total"] == 1
    assert body["lines"][0]["match_status"] == "matched"
    assert body["lines"][0]["amount"] == "2060000.00"
