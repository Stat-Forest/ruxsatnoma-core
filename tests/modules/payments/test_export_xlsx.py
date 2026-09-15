"""Stage 13 (ruling #204): the payments module's register exports are their
screen on paper — same scope, same filters, readable cells, the id last.

One "mirrors the list" test per register — the same rows as the list under
the same filter, labels rather than codes, the list's other filter, the cap —
plus the register's own answer to a caller with no scope."""

import csv
import hashlib
import io
import json
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.core.models import MediaFile
from app.main import create_app
from app.modules.admin.models import ClassifierItem
from app.modules.payments import statement_service
from app.modules.payments.models import Invoice
from tests.conftest import assert_export_cut, export_cap, make_client, xlsx_rows
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.gis.conftest import _commit_pending_before_requests

pytestmark = pytest.mark.asyncio


def _ids(content: bytes) -> set[str]:
    return {str(row[-1]) for row in xlsx_rows(content)[1]}


def _row(content: bytes, id_: str):
    return next(r for r in xlsx_rows(content)[1] if str(r[-1]) == id_)


# --- Bank-statement upload helpers, local to this file (mirrors
# tests/modules/payments/test_statement_import.py's own idiom) ---------------

_STATEMENT_COLUMN_MAP = {"amount": "Amount", "operation_date": "Date", "purpose": "Purpose"}
_STATEMENT_HEADER = ["Amount", "Date", "Purpose"]


def _statement_csv(*, amount: str = "1000.00", purpose: str = "test") -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(_STATEMENT_HEADER)
    writer.writerow([amount, "02.09.2026", purpose])
    return buffer.getvalue().encode("utf-8")


async def _upload_statement(client, *, amount: str = "1000.00", purpose: str = "test") -> str:
    resp = await client.post(
        "/api/v1/payments/bank-statements",
        data={"statement_date": "2026-09-02", "column_map": json.dumps(_STATEMENT_COLUMN_MAP)},
        files={"file": ("vypiska.csv", _statement_csv(amount=amount, purpose=purpose), "text/csv")},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["id"]


async def _drain(db) -> None:
    """Runs the queued parse job on the test's own session — the app's HTTP
    session already committed the upload, and workers are off in tests
    (lesson: build a fixture's precondition through the real transition)."""
    while await statement_service.process_pending(db):
        pass


@pytest.fixture
async def matchable_invoice(db, approved_application) -> Invoice:
    """An invoice numbered so the statement matcher can find it in free
    text (`matcher.INVOICE_NUMBER_RE` needs `INV-\\d{4}-\\d{6,}`) — mirrors
    `test_statement_import.py::bank_invoice`, duplicated here rather than
    imported cross-file to keep this test file self-contained."""
    row = Invoice(
        application_id=approved_application.id,
        number=f"INV-2026-{uuid.uuid4().int % 10**6:06d}",
        amount=Decimal("2060000.00"),
        status="pending",
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def open_reconciliation(payments_view_client, db, matchable_invoice) -> Invoice:
    """An OPEN `discrepancy` row for `matchable_invoice`, produced through
    the real upload + matcher path (mirrors `test_statement_import.py`'s
    own discrepancy test: an amount that disagrees with the invoice, in a
    statement whose purpose names it) — never a `Reconciliation(...)` row
    written by hand. Returns the invoice itself, the one deterministic key
    this register offers no id-filter for."""
    await _upload_statement(
        payments_view_client,
        amount="2 000 000,00",
        purpose=f"Оплата по счёту {matchable_invoice.number}",
    )
    await _drain(db)
    return matchable_invoice


@pytest.fixture
async def bank_doc(db) -> MediaFile:
    """A genuine `media_files` row — `bank_doc_file_id` is a NOT NULL FK
    (mirrors `test_manual_confirmation.py`'s own fixture of the same name)."""
    row = MediaFile(
        storage_key=f"bank-docs/{uuid.uuid4().hex}.pdf",
        filename="payment-order.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def filed_manual_confirmation(payments_view_client, pending_invoice, bank_doc) -> dict:
    """A real `pending_check` filing through `POST /payments/manual-
    confirmations` (mirrors `test_manual_confirmation.py::_file_via_http`)
    — never a `ManualPaymentConfirmation(...)` row written by hand."""
    resp = await payments_view_client.post(
        "/api/v1/payments/manual-confirmations",
        json={
            "invoice_id": str(pending_invoice.id),
            "amount": str(pending_invoice.amount),
            "paid_at": "2026-09-01T10:00:00Z",
            "bank_doc_file_id": str(bank_doc.id),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def checker_client(db) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in `executor_head` — the independent CHECKER half of the
    manual maker-checker door (`payments.confirm`, migration 0022) —
    mirrors `payments_view_client`'s own construction."""
    user = await make_user(db, role_code="executor_head")
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client


@pytest.fixture
async def paid_invoice_with_allocations(
    checker_client, filed_manual_confirmation, pending_invoice
) -> Invoice:
    """`filed_manual_confirmation`, CONFIRMED by an independent checker —
    writes real `allocations` rows through `payments.service.
    confirm_payment` (mirrors `test_manual_confirmation.py`'s own confirm
    test) — never an `Allocation(...)` row written by hand."""
    confirmed = await checker_client.post(
        f"/api/v1/payments/manual-confirmations/{filed_manual_confirmation['id']}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text
    return pending_invoice


@pytest.fixture
async def refund_basis(db) -> uuid.UUID:
    """The seeded `refund_reasons` item `RF-01` (migration `0022`), looked
    up by CODE — mirrors `test_refunds.py::rf01` (its own row id is
    `gen_random_uuid()` at migration time, so no literal is real across
    databases)."""
    return (
        await db.execute(select(ClassifierItem.id).where(ClassifierItem.code == "RF-01"))
    ).scalar_one()


@pytest.fixture
async def filed_refund(payments_view_client, pending_invoice, refund_basis) -> dict:
    """A real `requested` refund through `POST /refunds` (mirrors
    `test_refunds.py`'s own filing calls) — never a `Refund(...)` row
    written by hand."""
    resp = await payments_view_client.post(
        "/api/v1/refunds",
        json={
            "application_id": str(pending_invoice.application_id),
            "basis_item_id": str(refund_basis),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- /invoices/export.xlsx (Task C.1) ---------------------------------------

INVOICES = "/api/v1/invoices/export.xlsx"
STATEMENTS = "/api/v1/payments/bank-statements/export.xlsx"
RECONCILIATIONS = "/api/v1/payments/reconciliations/export.xlsx"
MANUAL_CONFIRMATIONS = "/api/v1/payments/manual-confirmations/export.xlsx"
ALLOCATIONS = "/api/v1/payments/allocations/export.xlsx"
REFUNDS = "/api/v1/refunds/export.xlsx"
RECIPIENTS = "/api/v1/payments/recipients/export.xlsx"


async def test_the_invoices_export_mirrors_the_list(payments_view_client, invoice):
    # Scoped by `application_id` — the same filter the screen's own "search
    # by application" box sends — so the comparison is deterministic
    # regardless of what earlier tests left in this shared, persistent test
    # database (lesson: "The test DB is shared, persistent, and never empty").
    by_application = {"application_id": str(invoice.application_id)}
    listed = (await payments_view_client.get("/api/v1/invoices", params=by_application)).json()
    listed_ids = {row["id"] for row in listed["items"]}
    assert listed_ids  # non-empty: this invoice is visible to a payments.view holder

    resp = await payments_view_client.get(INVOICES, params={**by_application, "lang": "ru"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.headers["x-export-truncated"] == "false"
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Номер счёта" and headers[-1] == "ID"
    assert {str(row[-1]) for row in rows} == listed_ids

    resp = await payments_view_client.get(INVOICES, params={**by_application, "lang": "uz_latn"})
    row = _row(resp.content, str(invoice.id))
    assert row[0] == invoice.number  # the human number first
    assert row[1] == "Toʻlov kutilmoqda"  # the status label, not "pending"

    resp = await payments_view_client.get(
        INVOICES, params={**by_application, "status": "paid", "lang": "uz_latn"}
    )
    assert resp.status_code == 200
    assert xlsx_rows(resp.content)[1] == []

    with export_cap(1):
        assert_export_cut(await payments_view_client.get(INVOICES), cap=1)


async def test_invoices_export_matches_the_list_status_for_a_caller_with_no_scope(owner_client):
    # `owner_client` with `application_id` omitted gets whatever `GET
    # /invoices` gives it today (currently 403 — `payments.view` gated,
    # backend-gaps finding 3; a later stage may change this to the owner's
    # own list, 200) — the export must answer the SAME status and, on a
    # 200, the same id set (brief shape 5).
    listed_resp = await owner_client.get("/api/v1/invoices")
    export_resp = await owner_client.get(INVOICES)
    assert export_resp.status_code == listed_resp.status_code
    if listed_resp.status_code == 200:
        listed_ids = {row["id"] for row in listed_resp.json()["items"]}
        assert _ids(export_resp.content) == listed_ids


# --- /payments/bank-statements/export.xlsx (Task C.2) -----------------------


async def test_the_statements_export_mirrors_the_list(payments_view_client, db):
    statement_id = await _upload_statement(payments_view_client)
    await _drain(db)

    listed = (
        await payments_view_client.get("/api/v1/payments/bank-statements", params={"limit": 200})
    ).json()
    listed_ids = {row["id"] for row in listed["items"]}
    assert statement_id in listed_ids

    resp = await payments_view_client.get(STATEMENTS, params={"lang": "ru"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Дата выписки" and headers[-1] == "ID"
    exported_ids = {str(row[-1]) for row in rows}
    assert statement_id in exported_ids
    assert exported_ids == listed_ids

    resp = await payments_view_client.get(
        STATEMENTS, params={"status": "parsed", "lang": "uz_latn"}
    )
    assert _row(resp.content, statement_id)[1] == "Qayta ishlandi"  # the label, not "parsed"

    resp = await payments_view_client.get(STATEMENTS, params={"status": "failed"})
    assert resp.status_code == 200
    assert statement_id not in _ids(resp.content)  # a "parsed" statement is not "failed"

    with export_cap(1):
        assert_export_cut(await payments_view_client.get(STATEMENTS), cap=1)


async def test_statements_export_matches_the_list_status_for_a_caller_with_no_scope(
    applicant_client,
):
    listed_resp = await applicant_client.get("/api/v1/payments/bank-statements")
    export_resp = await applicant_client.get(STATEMENTS)
    assert export_resp.status_code == listed_resp.status_code


# --- /payments/reconciliations/export.xlsx (Task C.2) -----------------------


async def test_the_reconciliations_export_mirrors_the_list(
    payments_view_client, open_reconciliation
):
    listed = (
        await payments_view_client.get(
            "/api/v1/payments/reconciliations", params={"status": "open", "limit": 200}
        )
    ).json()
    mine = next(
        item for item in listed["items"] if item["invoice_id"] == str(open_reconciliation.id)
    )
    listed_ids = {item["id"] for item in listed["items"]}

    resp = await payments_view_client.get(RECONCILIATIONS, params={"status": "open", "lang": "ru"})
    assert resp.status_code == 200
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Счёт" and headers[-1] == "ID"
    exported_ids = {str(row[-1]) for row in rows}
    assert mine["id"] in exported_ids
    assert exported_ids == listed_ids

    resp = await payments_view_client.get(
        RECONCILIATIONS, params={"status": "open", "lang": "uz_latn"}
    )
    row = _row(resp.content, mine["id"])
    assert row[0] == open_reconciliation.number
    assert row[1] == "Nomuvofiqlik"  # the result label, not "discrepancy"
    assert row[2] == "Ochiq"  # the status label, not "open"

    resp = await payments_view_client.get(RECONCILIATIONS, params={"status": "resolved"})
    assert resp.status_code == 200
    assert mine["id"] not in _ids(resp.content)

    with export_cap(1):
        assert_export_cut(await payments_view_client.get(RECONCILIATIONS), cap=1)


async def test_reconciliations_export_matches_the_list_status_for_a_caller_with_no_scope(
    applicant_client,
):
    listed_resp = await applicant_client.get("/api/v1/payments/reconciliations")
    export_resp = await applicant_client.get(RECONCILIATIONS)
    assert export_resp.status_code == listed_resp.status_code


# --- /payments/manual-confirmations/export.xlsx (Task C.2) ------------------


async def test_the_manual_confirmations_export_mirrors_the_list(
    payments_view_client, filed_manual_confirmation
):
    pending = {"status": "pending_check"}
    listed = (
        await payments_view_client.get(
            "/api/v1/payments/manual-confirmations", params={**pending, "limit": 200}
        )
    ).json()
    listed_ids = {item["id"] for item in listed["items"]}
    assert filed_manual_confirmation["id"] in listed_ids

    resp = await payments_view_client.get(MANUAL_CONFIRMATIONS, params={**pending, "lang": "ru"})
    assert resp.status_code == 200
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Счёт" and headers[-1] == "ID"
    exported_ids = {str(row[-1]) for row in rows}
    assert filed_manual_confirmation["id"] in exported_ids
    assert exported_ids == listed_ids

    resp = await payments_view_client.get(
        MANUAL_CONFIRMATIONS, params={**pending, "lang": "uz_latn"}
    )
    row = _row(resp.content, filed_manual_confirmation["id"])
    assert row[1] == "Tekshiruv kutilmoqda"  # the status label, not "pending_check"

    resp = await payments_view_client.get(MANUAL_CONFIRMATIONS, params={"status": "confirmed"})
    assert resp.status_code == 200
    assert filed_manual_confirmation["id"] not in _ids(resp.content)  # still "pending_check"

    with export_cap(1):
        assert_export_cut(await payments_view_client.get(MANUAL_CONFIRMATIONS), cap=1)


async def test_manual_confirmations_export_matches_the_list_status_for_a_caller_with_no_scope(
    applicant_client,
):
    listed_resp = await applicant_client.get("/api/v1/payments/manual-confirmations")
    export_resp = await applicant_client.get(MANUAL_CONFIRMATIONS)
    assert export_resp.status_code == listed_resp.status_code


# --- /payments/allocations/export.xlsx (Task C.2) ---------------------------


async def test_the_allocations_export_mirrors_the_list(
    payments_view_client, paid_invoice_with_allocations
):
    by_invoice = {"invoice_id": str(paid_invoice_with_allocations.id)}
    listed = (
        await payments_view_client.get(
            "/api/v1/payments/allocations", params={**by_invoice, "limit": 200}
        )
    ).json()
    listed_ids = {row["id"] for row in listed["items"]}
    assert listed_ids  # at least the leshoz's own remainder row

    resp = await payments_view_client.get(ALLOCATIONS, params={**by_invoice, "lang": "ru"})
    assert resp.status_code == 200
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Счёт" and headers[-1] == "ID"
    assert {str(row[-1]) for row in rows} == listed_ids

    resp = await payments_view_client.get(ALLOCATIONS, params={**by_invoice, "lang": "uz_latn"})
    (row, *_) = xlsx_rows(resp.content)[1]
    assert row[0] == paid_invoice_with_allocations.number  # the human number first
    assert row[1] == "Toʻlov"  # the entry-type label, not "payment"

    # An invoice id with no allocations written against it narrows the
    # export to zero rows exactly as it narrows the list — `list_allocations`
    # only ever filters `Allocation.invoice_id`, no existence check.
    resp = await payments_view_client.get(ALLOCATIONS, params={"invoice_id": str(uuid.uuid4())})
    assert resp.status_code == 200
    assert xlsx_rows(resp.content)[1] == []

    with export_cap(1):
        assert_export_cut(await payments_view_client.get(ALLOCATIONS, params=by_invoice), cap=1)


async def test_allocations_export_matches_the_list_status_for_a_caller_with_no_scope(
    applicant_client, paid_invoice_with_allocations
):
    by_invoice = {"invoice_id": str(paid_invoice_with_allocations.id)}
    listed_resp = await applicant_client.get("/api/v1/payments/allocations", params=by_invoice)
    export_resp = await applicant_client.get(ALLOCATIONS, params=by_invoice)
    assert export_resp.status_code == listed_resp.status_code


# --- /refunds/export.xlsx (Task C.2) -----------------------------------------


async def test_the_refunds_export_mirrors_the_list(payments_view_client, filed_refund):
    by_application = {"application_id": filed_refund["application_id"]}
    listed = (await payments_view_client.get("/api/v1/refunds", params=by_application)).json()
    listed_ids = {item["id"] for item in listed["items"]}
    assert filed_refund["id"] in listed_ids

    resp = await payments_view_client.get(REFUNDS, params={**by_application, "lang": "ru"})
    assert resp.status_code == 200
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Заявка" and headers[-1] == "ID"
    assert {str(row[-1]) for row in rows} == listed_ids

    resp = await payments_view_client.get(REFUNDS, params={**by_application, "lang": "uz_latn"})
    assert _row(resp.content, filed_refund["id"])[2] == "Soʻralgan"  # the status label

    resp = await payments_view_client.get(REFUNDS, params={**by_application, "status": "returned"})
    assert resp.status_code == 200
    assert xlsx_rows(resp.content)[1] == []

    with export_cap(1):
        assert_export_cut(await payments_view_client.get(REFUNDS), cap=1)


async def test_refunds_export_matches_the_list_status_for_a_caller_with_no_scope(
    applicant_client,
):
    listed_resp = await applicant_client.get("/api/v1/refunds")
    export_resp = await applicant_client.get(REFUNDS)
    assert export_resp.status_code == listed_resp.status_code


# --- /payments/recipients/export.xlsx (Task C.2) -----------------------------


async def test_the_recipients_export_mirrors_the_list(payments_view_client, budget_50):
    listed = (
        await payments_view_client.get("/api/v1/payments/recipients", params={"page_size": 100})
    ).json()
    listed_ids = {item["id"] for item in listed["items"]}
    assert str(budget_50.id) in listed_ids

    resp = await payments_view_client.get(RECIPIENTS, params={"lang": "ru"})
    assert resp.status_code == 200
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Название" and headers[-1] == "ID"
    assert {str(row[-1]) for row in rows} == listed_ids

    resp = await payments_view_client.get(RECIPIENTS, params={"lang": "uz_latn"})
    row = _row(resp.content, str(budget_50.id))
    assert row[0] == "Davlat byudjeti"  # the name first
    assert row[1] == "Foiz"  # the kind label, not "percent"
    assert row[5] == "Faol"  # the status label, not True

    with export_cap(1):
        assert_export_cut(await payments_view_client.get(RECIPIENTS), cap=1)


async def test_recipients_export_includes_inactive_rows_like_the_list(
    payments_view_client, budget_50_inactive
):
    # `payment_recipients` has no filter parameter at all (ruling #157: an
    # inactive row is never hidden) — this stands in for the filter shape,
    # proving the export does not silently narrow beyond what the list shows.
    resp = await payments_view_client.get(RECIPIENTS, params={"lang": "uz_latn"})
    assert resp.status_code == 200
    assert _row(resp.content, str(budget_50_inactive.id))[5] == "Faol emas"


async def test_recipients_export_matches_the_list_status_for_a_caller_with_no_scope(
    applicant_client,
):
    listed_resp = await applicant_client.get("/api/v1/payments/recipients")
    export_resp = await applicant_client.get(RECIPIENTS)
    assert export_resp.status_code == listed_resp.status_code
