"""`GET /payments/allocations` (plan `03.10b-payments-reconciliation` task
10) — the ledger read route: `payment`, `correction` and `refund` rows
alike, oldest first, selected either by ONE invoice or by an `occurred_at`
period, with `account` always present and `null` rather than omitted or
`""` (ruling 10, `tz/12` #15).

Allocations are built directly through the ORM, not through a real payment
or refund flow: this file is testing the ROUTE's own selection, ordering and
serialization, not any writer's business logic (those are `test_ledger.py`,
`test_reversal.py` and `test_refunds.py`'s own jobs). `occurred_at` is set
explicitly on each row — the column has a `server_default=func.now()`, which
would make every row in one test collide on the same instant and defeat the
`period_from`/`period_to` tests below.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models import Allocation, Invoice

ALLOCATIONS = "/api/v1/payments/allocations"


def _at(day: str) -> datetime:
    return datetime.fromisoformat(f"{day}T12:00:00+00:00")


async def test_the_ledger_for_one_invoice_is_oldest_first_and_untyped(
    payments_view_client, db: AsyncSession, invoice: Invoice
):
    """All three `entry_type` values for ONE invoice come back, in
    `occurred_at` order — never filtered down to `payment` alone."""
    payment = Allocation(
        invoice_id=invoice.id,
        entry_type="payment",
        target="recipient",
        account="20208000123456789012",
        amount=Decimal("100.00"),
        occurred_at=_at("2026-01-10"),
    )
    correction = Allocation(
        invoice_id=invoice.id,
        entry_type="correction",
        target="recipient",
        account="20208000123456789012",
        amount=Decimal("-100.00"),
        occurred_at=_at("2026-01-11"),
        note="reversed",
    )
    refund = Allocation(
        invoice_id=invoice.id,
        entry_type="refund",
        target="budget",
        account=None,
        amount=Decimal("-50.00"),
        occurred_at=_at("2026-01-12"),
        note="refund approved",
    )
    db.add_all([payment, correction, refund])
    await db.commit()

    response = await payments_view_client.get(ALLOCATIONS, params={"invoice_id": str(invoice.id)})
    assert response.status_code == 200, response.text
    body = response.json()
    ours = [item for item in body["items"] if item["invoice_id"] == str(invoice.id)]
    assert [item["entry_type"] for item in ours] == ["payment", "correction", "refund"]


async def test_account_is_null_never_omitted_never_blank(
    payments_view_client, db: AsyncSession, invoice: Invoice
):
    """Ruling 10, `tz/12` #15: the state budget's account number is stored
    nowhere in this system — `account` must serialize as JSON `null`
    (present as a key with no string value), never absent from the object
    and never an empty string."""
    row = Allocation(
        invoice_id=invoice.id,
        entry_type="payment",
        target="budget",
        account=None,
        amount=Decimal("50.00"),
        occurred_at=_at("2026-01-13"),
    )
    db.add(row)
    await db.commit()

    response = await payments_view_client.get(ALLOCATIONS, params={"invoice_id": str(invoice.id)})
    assert response.status_code == 200, response.text
    item = next(i for i in response.json()["items"] if i["id"] == str(row.id))
    assert "account" in item
    assert item["account"] is None


async def test_neither_invoice_id_nor_period_answers_err_val_001(payments_view_client):
    response = await payments_view_client.get(ALLOCATIONS)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ERR-VAL-001"


async def test_only_half_the_period_pair_answers_err_val_001(payments_view_client):
    response = await payments_view_client.get(ALLOCATIONS, params={"period_from": "2026-01-01"})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ERR-VAL-001"


async def test_a_reversed_period_answers_err_val_001(payments_view_client):
    response = await payments_view_client.get(
        ALLOCATIONS, params={"period_from": "2026-02-01", "period_to": "2026-01-01"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ERR-VAL-001"


async def test_a_period_query_finds_a_row_inside_it_and_excludes_one_outside(
    payments_view_client, db: AsyncSession, invoice: Invoice
):
    tag = uuid.uuid4().hex[:8]
    inside = Allocation(
        invoice_id=invoice.id,
        entry_type="payment",
        target="recipient",
        account="20208000123456789012",
        amount=Decimal("1.00"),
        occurred_at=_at("2026-03-15"),
        note=f"inside-{tag}",
    )
    outside = Allocation(
        invoice_id=invoice.id,
        entry_type="payment",
        target="recipient",
        account="20208000123456789012",
        amount=Decimal("1.00"),
        occurred_at=_at("2026-04-01"),
        note=f"outside-{tag}",
    )
    db.add_all([inside, outside])
    await db.commit()

    response = await payments_view_client.get(
        ALLOCATIONS, params={"period_from": "2026-03-01", "period_to": "2026-03-31"}
    )
    assert response.status_code == 200, response.text
    notes = {item["note"] for item in response.json()["items"]}
    assert f"inside-{tag}" in notes
    assert f"outside-{tag}" not in notes


async def test_an_applicant_may_not_read_the_ledger(applicant_client):
    response = await applicant_client.get(ALLOCATIONS)
    assert response.status_code == 403
