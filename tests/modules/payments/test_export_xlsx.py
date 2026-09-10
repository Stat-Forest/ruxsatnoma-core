"""Stage 13 (ruling #204): the payments module's register exports are their
screen on paper — same scope, same filters, readable cells, the id last.

`/invoices/export.xlsx` (Task C.1) below; the other payments lists (Task
C.2) land in this same file, one commit each."""

import csv
import hashlib
import io
import json
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from openpyxl import load_workbook

from app.core.models import MediaFile
from app.main import create_app
from app.modules.payments import statement_service
from app.modules.payments.models import Invoice
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.gis.conftest import _commit_pending_before_requests

pytestmark = pytest.mark.asyncio


def _sheet(content: bytes):
    sheet = load_workbook(io.BytesIO(content)).active
    assert sheet is not None  # a fresh Workbook always has one active sheet
    return sheet


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


# --- /invoices/export.xlsx (Task C.1) ---------------------------------------


async def test_invoices_export_holds_exactly_the_rows_the_list_shows(payments_view_client, invoice):
    # Scoped by `application_id` — the same filter the screen's own "search
    # by application" box sends — so the comparison is deterministic
    # regardless of what earlier tests left in this shared, persistent test
    # database (lesson: "The test DB is shared, persistent, and never empty").
    listed = (
        await payments_view_client.get(
            "/api/v1/invoices", params={"application_id": str(invoice.application_id)}
        )
    ).json()
    listed_ids = {row["id"] for row in listed["items"]}
    assert listed_ids  # non-empty: this invoice is visible to a payments.view holder

    resp = await payments_view_client.get(
        "/api/v1/invoices/export.xlsx",
        params={"application_id": str(invoice.application_id), "lang": "ru"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.headers["x-export-truncated"] == "false"
    sheet = _sheet(resp.content)
    headers = [c.value for c in sheet[1]]
    assert headers[0] == "Номер счёта" and headers[-1] == "ID"
    exported_ids = {str(row[-1]) for row in sheet.iter_rows(min_row=2, values_only=True)}
    assert exported_ids == listed_ids


async def test_invoices_export_applies_the_same_filters_as_the_list(payments_view_client, invoice):
    resp = await payments_view_client.get(
        "/api/v1/invoices/export.xlsx",
        params={"application_id": str(invoice.application_id), "status": "paid", "lang": "uz_latn"},
    )
    assert resp.status_code == 200
    assert list(_sheet(resp.content).iter_rows(min_row=2, values_only=True)) == []


async def test_invoices_export_renders_labels_not_codes(payments_view_client, invoice):
    resp = await payments_view_client.get(
        "/api/v1/invoices/export.xlsx",
        params={"application_id": str(invoice.application_id), "lang": "uz_latn"},
    )
    row = next(_sheet(resp.content).iter_rows(min_row=2, values_only=True))
    assert row[0] == invoice.number  # the human number first
    assert row[1] == "Toʻlov kutilmoqda"  # the status label, not "pending"


async def test_invoices_export_truncates_at_the_cap_and_says_so(
    payments_view_client, invoice, monkeypatch
):
    from app.core import settings_store

    original_get_int = settings_store.get_int

    async def capped(db, key):
        # `get_current_session` (every authenticated request) also reads
        # `session_idle_minutes` through this same function — only the
        # export's own cap key is overridden here.
        if key == "register_export_max_rows":
            return 1
        return await original_get_int(db, key)

    monkeypatch.setattr(settings_store, "get_int", capped)
    resp = await payments_view_client.get("/api/v1/invoices/export.xlsx")
    assert resp.status_code == 200
    total = int(resp.headers["x-export-total"])
    assert resp.headers["x-export-truncated"] == ("true" if total > 1 else "false")
    assert len(list(_sheet(resp.content).iter_rows(min_row=2, values_only=True))) <= 1


async def test_invoices_export_matches_the_list_status_for_a_caller_with_no_scope(owner_client):
    # `owner_client` with `application_id` omitted gets whatever `GET
    # /invoices` gives it today (currently 403 — `payments.view` gated,
    # backend-gaps finding 3; a later stage may change this to the owner's
    # own list, 200) — the export must answer the SAME status and, on a
    # 200, the same id set (brief shape 5).
    listed_resp = await owner_client.get("/api/v1/invoices")
    export_resp = await owner_client.get("/api/v1/invoices/export.xlsx")
    assert export_resp.status_code == listed_resp.status_code
    if listed_resp.status_code == 200:
        listed_ids = {row["id"] for row in listed_resp.json()["items"]}
        exported_ids = {
            str(row[-1])
            for row in _sheet(export_resp.content).iter_rows(min_row=2, values_only=True)
        }
        assert exported_ids == listed_ids


async def test_invoices_export_rejects_an_unknown_language(payments_view_client):
    resp = await payments_view_client.get("/api/v1/invoices/export.xlsx", params={"lang": "en"})
    assert resp.status_code == 422


# --- /payments/bank-statements/export.xlsx (Task C.2) -----------------------


async def test_statements_export_holds_exactly_the_rows_the_list_shows(payments_view_client, db):
    statement_id = await _upload_statement(payments_view_client)
    await _drain(db)

    listed = (
        await payments_view_client.get("/api/v1/payments/bank-statements", params={"limit": 200})
    ).json()
    listed_ids = {row["id"] for row in listed["items"]}
    assert statement_id in listed_ids

    resp = await payments_view_client.get(
        "/api/v1/payments/bank-statements/export.xlsx", params={"lang": "ru"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    sheet = _sheet(resp.content)
    headers = [c.value for c in sheet[1]]
    assert headers[0] == "Дата выписки" and headers[-1] == "ID"
    exported_ids = {str(row[-1]) for row in sheet.iter_rows(min_row=2, values_only=True)}
    assert statement_id in exported_ids
    assert exported_ids == listed_ids


async def test_statements_export_applies_the_same_filters_as_the_list(payments_view_client, db):
    statement_id = await _upload_statement(payments_view_client)
    await _drain(db)

    resp = await payments_view_client.get(
        "/api/v1/payments/bank-statements/export.xlsx", params={"status": "failed"}
    )
    assert resp.status_code == 200
    exported_ids = {
        str(row[-1]) for row in _sheet(resp.content).iter_rows(min_row=2, values_only=True)
    }
    assert statement_id not in exported_ids  # a "parsed" statement is not "failed"


async def test_statements_export_renders_labels_not_codes(payments_view_client, db):
    statement_id = await _upload_statement(payments_view_client)
    await _drain(db)

    resp = await payments_view_client.get(
        "/api/v1/payments/bank-statements/export.xlsx",
        params={"status": "parsed", "lang": "uz_latn"},
    )
    row_by_id = {
        str(row[-1]): row for row in _sheet(resp.content).iter_rows(min_row=2, values_only=True)
    }
    assert row_by_id[statement_id][1] == "Qayta ishlandi"  # the label, not "parsed"


async def test_statements_export_truncates_at_the_cap_and_says_so(
    payments_view_client, monkeypatch
):
    from app.core import settings_store

    original_get_int = settings_store.get_int

    async def capped(db, key):
        if key == "register_export_max_rows":
            return 1
        return await original_get_int(db, key)

    monkeypatch.setattr(settings_store, "get_int", capped)
    resp = await payments_view_client.get("/api/v1/payments/bank-statements/export.xlsx")
    assert resp.status_code == 200
    total = int(resp.headers["x-export-total"])
    assert resp.headers["x-export-truncated"] == ("true" if total > 1 else "false")
    assert len(list(_sheet(resp.content).iter_rows(min_row=2, values_only=True))) <= 1


async def test_statements_export_matches_the_list_status_for_a_caller_with_no_scope(
    applicant_client,
):
    listed_resp = await applicant_client.get("/api/v1/payments/bank-statements")
    export_resp = await applicant_client.get("/api/v1/payments/bank-statements/export.xlsx")
    assert export_resp.status_code == listed_resp.status_code


async def test_statements_export_rejects_an_unknown_language(payments_view_client):
    resp = await payments_view_client.get(
        "/api/v1/payments/bank-statements/export.xlsx", params={"lang": "en"}
    )
    assert resp.status_code == 422


# --- /payments/reconciliations/export.xlsx (Task C.2) -----------------------


async def test_reconciliations_export_holds_exactly_the_rows_the_list_shows(
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

    resp = await payments_view_client.get(
        "/api/v1/payments/reconciliations/export.xlsx", params={"status": "open", "lang": "ru"}
    )
    assert resp.status_code == 200
    sheet = _sheet(resp.content)
    headers = [c.value for c in sheet[1]]
    assert headers[0] == "Счёт" and headers[-1] == "ID"
    exported_ids = {str(row[-1]) for row in sheet.iter_rows(min_row=2, values_only=True)}
    assert mine["id"] in exported_ids
    assert exported_ids == listed_ids


async def test_reconciliations_export_applies_the_same_filters_as_the_list(
    payments_view_client, open_reconciliation
):
    resp = await payments_view_client.get(
        "/api/v1/payments/reconciliations/export.xlsx", params={"status": "resolved"}
    )
    assert resp.status_code == 200
    exported_numbers = {
        str(row[0]) for row in _sheet(resp.content).iter_rows(min_row=2, values_only=True)
    }
    assert open_reconciliation.number not in exported_numbers


async def test_reconciliations_export_renders_labels_not_codes(
    payments_view_client, open_reconciliation
):
    resp = await payments_view_client.get(
        "/api/v1/payments/reconciliations/export.xlsx",
        params={"status": "open", "lang": "uz_latn"},
    )
    rows_by_invoice_number = {
        row[0]: row for row in _sheet(resp.content).iter_rows(min_row=2, values_only=True)
    }
    row = rows_by_invoice_number[open_reconciliation.number]
    assert row[1] == "Nomuvofiqlik"  # the result label, not "discrepancy"
    assert row[2] == "Ochiq"  # the status label, not "open"


async def test_reconciliations_export_truncates_at_the_cap_and_says_so(
    payments_view_client, open_reconciliation, monkeypatch
):
    from app.core import settings_store

    original_get_int = settings_store.get_int

    async def capped(db, key):
        if key == "register_export_max_rows":
            return 1
        return await original_get_int(db, key)

    monkeypatch.setattr(settings_store, "get_int", capped)
    resp = await payments_view_client.get("/api/v1/payments/reconciliations/export.xlsx")
    assert resp.status_code == 200
    total = int(resp.headers["x-export-total"])
    assert resp.headers["x-export-truncated"] == ("true" if total > 1 else "false")
    assert len(list(_sheet(resp.content).iter_rows(min_row=2, values_only=True))) <= 1


async def test_reconciliations_export_matches_the_list_status_for_a_caller_with_no_scope(
    applicant_client,
):
    listed_resp = await applicant_client.get("/api/v1/payments/reconciliations")
    export_resp = await applicant_client.get("/api/v1/payments/reconciliations/export.xlsx")
    assert export_resp.status_code == listed_resp.status_code


async def test_reconciliations_export_rejects_an_unknown_language(payments_view_client):
    resp = await payments_view_client.get(
        "/api/v1/payments/reconciliations/export.xlsx", params={"lang": "en"}
    )
    assert resp.status_code == 422


# --- /payments/manual-confirmations/export.xlsx (Task C.2) ------------------


async def test_manual_confirmations_export_holds_exactly_the_rows_the_list_shows(
    payments_view_client, filed_manual_confirmation
):
    listed = (
        await payments_view_client.get(
            "/api/v1/payments/manual-confirmations",
            params={"status": "pending_check", "limit": 200},
        )
    ).json()
    listed_ids = {item["id"] for item in listed["items"]}
    assert filed_manual_confirmation["id"] in listed_ids

    resp = await payments_view_client.get(
        "/api/v1/payments/manual-confirmations/export.xlsx",
        params={"status": "pending_check", "lang": "ru"},
    )
    assert resp.status_code == 200
    sheet = _sheet(resp.content)
    headers = [c.value for c in sheet[1]]
    assert headers[0] == "Счёт" and headers[-1] == "ID"
    exported_ids = {str(row[-1]) for row in sheet.iter_rows(min_row=2, values_only=True)}
    assert filed_manual_confirmation["id"] in exported_ids
    assert exported_ids == listed_ids


async def test_manual_confirmations_export_applies_the_same_filters_as_the_list(
    payments_view_client, filed_manual_confirmation
):
    resp = await payments_view_client.get(
        "/api/v1/payments/manual-confirmations/export.xlsx", params={"status": "confirmed"}
    )
    assert resp.status_code == 200
    exported_ids = {
        str(row[-1]) for row in _sheet(resp.content).iter_rows(min_row=2, values_only=True)
    }
    assert filed_manual_confirmation["id"] not in exported_ids  # still "pending_check"


async def test_manual_confirmations_export_renders_labels_not_codes(
    payments_view_client, filed_manual_confirmation
):
    resp = await payments_view_client.get(
        "/api/v1/payments/manual-confirmations/export.xlsx",
        params={"status": "pending_check", "lang": "uz_latn"},
    )
    rows_by_id = {
        str(row[-1]): row for row in _sheet(resp.content).iter_rows(min_row=2, values_only=True)
    }
    row = rows_by_id[filed_manual_confirmation["id"]]
    assert row[1] == "Tekshiruv kutilmoqda"  # the status label, not "pending_check"


async def test_manual_confirmations_export_truncates_at_the_cap_and_says_so(
    payments_view_client, filed_manual_confirmation, monkeypatch
):
    from app.core import settings_store

    original_get_int = settings_store.get_int

    async def capped(db, key):
        if key == "register_export_max_rows":
            return 1
        return await original_get_int(db, key)

    monkeypatch.setattr(settings_store, "get_int", capped)
    resp = await payments_view_client.get("/api/v1/payments/manual-confirmations/export.xlsx")
    assert resp.status_code == 200
    total = int(resp.headers["x-export-total"])
    assert resp.headers["x-export-truncated"] == ("true" if total > 1 else "false")
    assert len(list(_sheet(resp.content).iter_rows(min_row=2, values_only=True))) <= 1


async def test_manual_confirmations_export_matches_the_list_status_for_a_caller_with_no_scope(
    applicant_client,
):
    listed_resp = await applicant_client.get("/api/v1/payments/manual-confirmations")
    export_resp = await applicant_client.get("/api/v1/payments/manual-confirmations/export.xlsx")
    assert export_resp.status_code == listed_resp.status_code


async def test_manual_confirmations_export_rejects_an_unknown_language(payments_view_client):
    resp = await payments_view_client.get(
        "/api/v1/payments/manual-confirmations/export.xlsx", params={"lang": "en"}
    )
    assert resp.status_code == 422


# --- /payments/allocations/export.xlsx (Task C.2) ---------------------------


async def test_allocations_export_holds_exactly_the_rows_the_list_shows(
    payments_view_client, paid_invoice_with_allocations
):
    invoice_id = str(paid_invoice_with_allocations.id)
    listed = (
        await payments_view_client.get(
            "/api/v1/payments/allocations", params={"invoice_id": invoice_id, "limit": 200}
        )
    ).json()
    listed_ids = {row["id"] for row in listed["items"]}
    assert listed_ids  # at least the leshoz's own remainder row

    resp = await payments_view_client.get(
        "/api/v1/payments/allocations/export.xlsx",
        params={"invoice_id": invoice_id, "lang": "ru"},
    )
    assert resp.status_code == 200
    sheet = _sheet(resp.content)
    headers = [c.value for c in sheet[1]]
    assert headers[0] == "Счёт" and headers[-1] == "ID"
    exported_ids = {str(row[-1]) for row in sheet.iter_rows(min_row=2, values_only=True)}
    assert exported_ids == listed_ids


async def test_allocations_export_applies_the_same_filters_as_the_list(
    payments_view_client, paid_invoice_with_allocations
):
    # An invoice id with no allocations written against it narrows the
    # export to zero rows exactly as it narrows the list — `list_allocations`
    # only ever filters `Allocation.invoice_id`, no existence check.
    resp = await payments_view_client.get(
        "/api/v1/payments/allocations/export.xlsx", params={"invoice_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 200
    assert list(_sheet(resp.content).iter_rows(min_row=2, values_only=True)) == []


async def test_allocations_export_renders_labels_not_codes(
    payments_view_client, paid_invoice_with_allocations
):
    resp = await payments_view_client.get(
        "/api/v1/payments/allocations/export.xlsx",
        params={"invoice_id": str(paid_invoice_with_allocations.id), "lang": "uz_latn"},
    )
    row = next(_sheet(resp.content).iter_rows(min_row=2, values_only=True))
    assert row[0] == paid_invoice_with_allocations.number  # the human number first
    assert row[1] == "Toʻlov"  # the entry-type label, not "payment"


async def test_allocations_export_truncates_at_the_cap_and_says_so(
    payments_view_client, paid_invoice_with_allocations, monkeypatch
):
    from app.core import settings_store

    original_get_int = settings_store.get_int

    async def capped(db, key):
        if key == "register_export_max_rows":
            return 1
        return await original_get_int(db, key)

    monkeypatch.setattr(settings_store, "get_int", capped)
    resp = await payments_view_client.get(
        "/api/v1/payments/allocations/export.xlsx",
        params={"invoice_id": str(paid_invoice_with_allocations.id)},
    )
    assert resp.status_code == 200
    total = int(resp.headers["x-export-total"])
    assert resp.headers["x-export-truncated"] == ("true" if total > 1 else "false")
    assert len(list(_sheet(resp.content).iter_rows(min_row=2, values_only=True))) <= 1


async def test_allocations_export_matches_the_list_status_for_a_caller_with_no_scope(
    applicant_client, paid_invoice_with_allocations
):
    invoice_id = str(paid_invoice_with_allocations.id)
    listed_resp = await applicant_client.get(
        "/api/v1/payments/allocations", params={"invoice_id": invoice_id}
    )
    export_resp = await applicant_client.get(
        "/api/v1/payments/allocations/export.xlsx", params={"invoice_id": invoice_id}
    )
    assert export_resp.status_code == listed_resp.status_code


async def test_allocations_export_rejects_an_unknown_language(payments_view_client):
    resp = await payments_view_client.get(
        "/api/v1/payments/allocations/export.xlsx", params={"lang": "en"}
    )
    assert resp.status_code == 422
