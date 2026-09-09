"""RI-07 — the refund SLA sweep (plan `03.10b-payments-reconciliation` task
10, `tz/10`, `payments.jobs.refund_sla_sweep`).

Mirrors `test_expiry.py`'s own shape: the sweep is driven directly against an
already-open session (`jobs.refund_sla_sweep(db)`), never through
`app/workers/jobs.py`'s thin wrapper, and every idempotency assertion is
scoped to the ONE refund this file created — the shared, persistent test
database already carries refunds other files left behind (`test_refunds.py`'s
own long tail), some of them very possibly past their own `due_at` too, so a
bare `counts["flagged"]` after a second run is not by itself proof of
anything: only a query scoped to `overdue_refund.id` is.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import business_today
from app.modules.audit.models import AuditLog
from app.modules.payments import jobs
from app.modules.payments.jobs import REFUND_SLA_BREACH, RISK_INDICATOR_REFUND_OVERDUE
from app.modules.payments.models import Invoice, Refund, RefundComponent
from tests.modules.payments.test_refunds import rf01 as rf01


@pytest.fixture
async def overdue_refund(db: AsyncSession, invoice: Invoice, rf01: uuid.UUID) -> Refund:
    """A `requested` refund whose 20-working-day control deadline is already
    in the past — RI-07's own candidate, built directly (mirrors
    `test_backoffice_models.py`'s own bare `Refund(...)` rows): what matters
    here is the DATE, not how the row reached `requested`."""
    row = Refund(
        application_id=invoice.application_id,
        invoice_id=invoice.id,
        basis_item_id=rf01,
        status="requested",
        requested_at=datetime.now(UTC),
        due_at=business_today() - timedelta(days=1),
    )
    db.add(row)
    await db.flush()
    return row


async def _ri07_count(db: AsyncSession, refund_id: uuid.UUID) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == REFUND_SLA_BREACH, AuditLog.object_id == refund_id)
    )
    assert count is not None
    return count


async def test_an_overdue_refund_gets_one_ri_07_row(db: AsyncSession, overdue_refund: Refund):
    counts = await jobs.refund_sla_sweep(db)
    assert counts["flagged"] >= 1
    assert await _ri07_count(db, overdue_refund.id) == 1

    row = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == REFUND_SLA_BREACH, AuditLog.object_id == overdue_refund.id
            )
        )
    ).scalar_one()
    assert row.result == "success"
    assert row.user_id is None
    assert row.object_type == "refund"
    assert row.extra is not None
    assert row.extra["risk_indicator"] == RISK_INDICATOR_REFUND_OVERDUE


async def test_a_second_run_of_the_sweep_writes_no_second_row(
    db: AsyncSession, overdue_refund: Refund
):
    await jobs.refund_sla_sweep(db)
    await jobs.refund_sla_sweep(db)

    assert await _ri07_count(db, overdue_refund.id) == 1


@pytest.mark.parametrize("status", ["returned", "rejected"])
async def test_a_decided_refund_past_due_is_untouched(
    db: AsyncSession, overdue_refund: Refund, status: str
):
    """Only `requested`/`in_review` refunds are candidates — a `returned` or
    `rejected` one is terminal, however long past its own `due_at`, and the
    sweep must not raise RI-07 on a question that is already settled."""
    if status == "returned":
        # `refund_components_complete` (migration 0046) now requires at
        # least one component summing to `final_amount` for a `returned`
        # refund. Inserted in its OWN flush, before `status`/`final_amount`
        # change on `overdue_refund` — the trigger fires BEFORE UPDATE on
        # `refunds` and reads `refund_components` as it stands at that
        # moment, so the component must already be committed to the
        # session when `status` flips, not merely staged in the same
        # flush. `final_amount` and `status` are then set TOGETHER, in one
        # flush, so the trigger sees a `final_amount` that is not NULL and
        # a total that sums to it (satisfied trivially — the sweep's own
        # candidate filter is what this test is pinning, not the trigger).
        db.add(RefundComponent(refund_id=overdue_refund.id, amount=Decimal("100.00")))
        await db.flush()
        overdue_refund.final_amount = Decimal("100.00")
    overdue_refund.status = status
    await db.flush()

    await jobs.refund_sla_sweep(db)

    assert await _ri07_count(db, overdue_refund.id) == 0


async def test_a_refund_not_yet_past_due_is_untouched(db: AsyncSession, invoice: Invoice, rf01):
    row = Refund(
        application_id=invoice.application_id,
        invoice_id=invoice.id,
        basis_item_id=rf01,
        status="requested",
        requested_at=datetime.now(UTC),
        due_at=business_today() + timedelta(days=1),
    )
    db.add(row)
    await db.flush()

    await jobs.refund_sla_sweep(db)

    assert await _ri07_count(db, row.id) == 0
