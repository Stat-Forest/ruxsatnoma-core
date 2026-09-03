"""`GET /payments/reconciliations` and `POST
/payments/reconciliations/{id}/resolve` — the discrepancy register itself
(plan `03.10b-payments-reconciliation` task 5).

Both routes read/write a table that is SHARED and PERSISTENT across test
runs (`test_statement_import.py`'s own module docstring) — Task 4's own
suite has already committed a long tail of `open`/`resolved` rows through
real bank-statement imports, and it keeps growing every time this file
itself runs. `_find_in_register` below pages through the register rather
than trusting the first page to hold any one row, the same reasoning
`test_statement_import.py::_statements` gives for reading the shared table
directly instead of by position.
"""

import uuid

from sqlalchemy import select

from app.modules.audit.models import AuditLog
from app.modules.payments.backoffice_service import RESOLVE_ACTION
from app.modules.payments.models import BankStatementLine, Reconciliation
from tests.modules.payments.test_statement_import import csv_bytes, drain, upload
from tests.modules.payments.test_statement_import import row as csv_row


async def _make_reconciliation(
    db, *, status: str = "open", result: str = "unknown"
) -> Reconciliation:
    row_ = Reconciliation(result=result, status=status)
    db.add(row_)
    await db.flush()
    return row_


async def _find_in_register(client, *, target_id: uuid.UUID, status: str | None = None) -> dict:
    """Page through the register (`limit=200`, the route's own ceiling) until
    `target_id` turns up. Raises if the register's own `total` is exhausted
    first — a real failure, never a false negative caused by a fixed page."""
    limit = 200
    offset = 0
    while True:
        params: dict[str, int | str] = {"limit": limit, "offset": offset}
        if status is not None:
            params["status"] = status
        response = await client.get("/api/v1/payments/reconciliations", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        for item in body["items"]:
            if item["id"] == str(target_id):
                return item
        offset += limit
        if offset >= body["total"]:
            raise AssertionError(f"{target_id} never appeared in the register (status={status})")


# --- the register itself -----------------------------------------------------


async def test_the_register_lists_only_open_by_default_and_accepts_status_resolved(
    payments_view_client, db
):
    open_row = await _make_reconciliation(db, status="open")
    resolved_row = await _make_reconciliation(db, status="resolved", result="matched")

    default_response = await payments_view_client.get("/api/v1/payments/reconciliations")
    assert default_response.status_code == 200, default_response.text
    # Every item the default call actually returns is `open` — the WHERE
    # clause this checks, not merely that OUR row happens to be open.
    assert all(item["status"] == "open" for item in default_response.json()["items"])

    open_item = await _find_in_register(payments_view_client, target_id=open_row.id, status="open")
    assert open_item["status"] == "open"

    resolved_item = await _find_in_register(
        payments_view_client, target_id=resolved_row.id, status="resolved"
    )
    assert resolved_item["status"] == "resolved"


async def test_an_applicant_may_not_read_the_register(applicant_client):
    response = await applicant_client.get("/api/v1/payments/reconciliations")
    assert response.status_code == 403


# --- resolving ----------------------------------------------------------------


async def test_resolve_requires_a_nonempty_comment(payments_view_client, db):
    """`tz/08`: close with a comment or a correcting document — never with
    neither. A missing field is FastAPI's own validation (`ERR-VAL-001` with
    no `reason`, per `app.main.validation_error_handler`); a comment that is
    present but blank is the service's own check (`details.reason ==
    "comment_required"`), since a Pydantic `str` requirement cannot see past
    whitespace the way `str.strip()` can."""
    missing = await _make_reconciliation(db)
    blank = await _make_reconciliation(db)

    no_field = await payments_view_client.post(
        f"/api/v1/payments/reconciliations/{missing.id}/resolve", json={}
    )
    assert no_field.status_code == 422, no_field.text
    assert no_field.json()["error"]["code"] == "ERR-VAL-001"

    blank_comment = await payments_view_client.post(
        f"/api/v1/payments/reconciliations/{blank.id}/resolve",
        json={"comment": "   "},
    )
    assert blank_comment.status_code == 422, blank_comment.text
    assert blank_comment.json()["error"]["code"] == "ERR-VAL-001"
    assert blank_comment.json()["error"]["details"]["reason"] == "comment_required"


async def test_resolve_on_an_already_resolved_row_answers_err_pay_005(payments_view_client, db):
    """409, never a silent second resolution — `ERR-PAY-005` specifically
    (registered by Task 1 for exactly this case), NOT `ERR-PAY-004`, whose
    registered message names an invoice, not a reconciliation."""
    row = await _make_reconciliation(db, status="resolved", result="matched")

    response = await payments_view_client.post(
        f"/api/v1/payments/reconciliations/{row.id}/resolve",
        json={"comment": "already closed earlier"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ERR-PAY-005"


async def test_an_applicant_may_not_resolve_a_reconciliation(applicant_client, db):
    """`applicant` holds neither `payments.view` nor `payments.manage` —
    the same 403 `test_an_applicant_may_not_upload_a_bank_statement` proves
    for the sibling upload route."""
    row = await _make_reconciliation(db)
    response = await applicant_client.post(
        f"/api/v1/payments/reconciliations/{row.id}/resolve",
        json={"comment": "irrelevant — must never be reached"},
    )
    assert response.status_code == 403


async def test_resolve_writes_an_audit_row_with_the_comment_as_basis(payments_view_client, db):
    row = await _make_reconciliation(db)
    comment = f"bank confirmed the transfer by phone, ref {uuid.uuid4().hex[:8]}"

    response = await payments_view_client.post(
        f"/api/v1/payments/reconciliations/{row.id}/resolve",
        json={"comment": comment},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "resolved"
    assert body["comment"] == comment
    assert body["resolved_at"] is not None
    assert body["resolved_by"] is not None

    audited = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.object_id == row.id, AuditLog.action == RESOLVE_ACTION
                )
            )
        )
        .scalars()
        .one()
    )
    assert audited.basis == comment
    assert audited.result == "success"


# --- the deferred assertion: a REAL import's own row, through the GET route --


async def test_a_reconciliation_row_an_import_actually_wrote_comes_back_through_get(
    payments_view_client, db
):
    """Driven end to end: upload a statement whose one line names no invoice
    (Task 4's own `unknown_payment` shape), drain the job queue, and read the
    row it produced back through THIS task's own `GET
    /payments/reconciliations?status=open` — never a hand-built
    `Reconciliation(...)` row, which every other test in this file uses on
    purpose to stay independent of the importer."""
    response = await upload(
        payments_view_client,
        csv_bytes(csv_row(purpose="Оплата за услуги, без номера счёта")),
    )
    statement_id = uuid.UUID(response.json()["id"])
    await drain(db)

    line = (
        await db.execute(
            select(BankStatementLine).where(BankStatementLine.statement_id == statement_id)
        )
    ).scalar_one()
    assert line.match_status == "unknown_payment"
    reconciliation = (
        await db.execute(select(Reconciliation).where(Reconciliation.statement_line_id == line.id))
    ).scalar_one()
    assert reconciliation.status == "open"

    item = await _find_in_register(payments_view_client, target_id=reconciliation.id, status="open")
    assert item["statement_line_id"] == str(line.id)
    assert item["result"] == "unknown"
    assert item["invoice_id"] is None
