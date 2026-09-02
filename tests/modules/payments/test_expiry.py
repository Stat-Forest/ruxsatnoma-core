"""The daily sweep that closes an unpaid invoice's 10-day window (plan
`03.10a-payments-core` task 6, ruling 13).

The brief's own four tests, verbatim except for two adaptations:

- Its own `test_a_reminder_goes_out_before_the_deadline_once` asserts the
  FLAT `event_code == "invoice_due_soon"`; the DOTTED `invoice.due_soon` is
  the only form a seeded `notification_templates` row is looked up by
  (`payments.events`'s own docstring, task-6 ruling) — fixed here, the
  brief's surrounding prose was already right about which vocabulary to use.
- `test_an_unpaid_invoice_expires_together_with_its_application` checks the
  application card over `GET /api/v1/applications/{id}` in the brief, but
  `applications` has no router.py on this branch yet (`conftest.py`'s own
  note: "applications has no HTTP surface yet on dev", 3.9a branch 1 only,
  same reasoning `tests/modules/payments/test_invoice.py`'s own tests were
  already adapted for) — checked instead through `applications.service.get`,
  the exact cross-module read `test_invoice.py::
  test_the_application_moves_to_invoiced` already uses for the identical
  purpose.

Plus this module's own copy of the template guard test (3.9a's version
iterates `applications.events.NOTIFIED_EVENT_CODES` only and will never see
this module's codes), and a test that a leftover row whose application
cannot legally transition does not abort the sweep (ruling).
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications.models import Application
from app.modules.payments.models import Invoice


def _inv_number(label: str) -> str:
    """Task 4's `_tx_id()`-style generator, this file's own vocabulary: every
    literal a committing client leaves behind must be unique per run (the
    shared test DB lesson) — `uq_invoices_number` would otherwise collide
    with a leftover row from an earlier run of this same file."""
    return f"INV-2027-{label}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def overdue_invoice(db: AsyncSession, pending_invoice: Invoice) -> Invoice:
    """`pending_invoice` (`conftest.py`) is built through the REAL
    APPLICATION_APPROVED -> issue_invoice path, so its application is
    genuinely INVOICED — backdated here past its own 10-day window. 'Past
    due' is a plain column value, not a distinct business transition of its
    own (mirrors `conftest.py`'s own `expired_invoice`, task 5)."""
    now = datetime.now(UTC)
    pending_invoice.issued_at = now - timedelta(days=20)
    pending_invoice.due_at = now - timedelta(days=10)
    await db.flush()
    return pending_invoice


@pytest.fixture
async def paid_invoice(db: AsyncSession, approved_application: Application) -> Invoice:
    """A `paid` invoice, well past what would have been its own due date —
    the sweep's negative case: a paid invoice is never touched, however
    overdue it looks."""
    now = datetime.now(UTC)
    row = Invoice(
        application_id=approved_application.id,
        number=_inv_number("paid"),
        amount=Decimal("100.00"),
        status="paid",
        issued_at=now - timedelta(days=20),
        due_at=now - timedelta(days=10),
        paid_at=now - timedelta(days=15),
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def invoice_due_in_two_days(db: AsyncSession, pending_invoice: Invoice) -> Invoice:
    """`pending_invoice`, due in 2 days — inside `REMINDER_DAYS_BEFORE_DUE`'s
    3-day window, so the sweep's reminder pass notifies exactly once."""
    now = datetime.now(UTC)
    pending_invoice.issued_at = now - timedelta(days=8)
    pending_invoice.due_at = now + timedelta(days=2)
    await db.flush()
    return pending_invoice


@pytest.fixture
async def non_invoiced_overdue_invoice(
    db: AsyncSession, approved_without_calculation: Application
) -> Invoice:
    """A `pending` invoice past its own 10-day window whose application
    never reached INVOICED — stuck at APPROVED.
    `set_status(..., "EXPIRED_UNPAID")` refuses this transition
    (`APPLICATION_TRANSITIONS["APPROVED"]` has no such target): the sweep's
    own "cannot legally transition" case (ruling) — a row shaped exactly
    like this, left behind by some unrelated test in this shared database,
    must never abort a real nightly run. Built on
    `approved_without_calculation` rather than `approved_application`
    (`overdue_invoice`'s own base fixture) so the two never collide onto the
    SAME application when one test asks for both — pytest caches a
    function-scoped fixture once per test, and `issue_invoice`'s own
    idempotency check would otherwise turn `pending_invoice`'s publish into
    a no-op that finds this fixture's invoice already "in force"."""
    now = datetime.now(UTC)
    row = Invoice(
        application_id=approved_without_calculation.id,
        number=_inv_number("stuck-approved"),
        amount=Decimal("100.00"),
        status="pending",
        issued_at=now - timedelta(days=20),
        due_at=now - timedelta(days=10),
    )
    db.add(row)
    await db.flush()
    return row


async def test_an_unpaid_invoice_expires_together_with_its_application(db, overdue_invoice):
    """Ruling 13: half-applied expiry is the state that leaves an applicant
    unable to pay and unable to refile."""
    from app.modules.applications import service as applications_service
    from app.modules.payments import jobs

    await jobs.expiry_sweep(db)

    await db.refresh(overdue_invoice)
    assert overdue_invoice.status == "expired"
    application = await applications_service.get(db, overdue_invoice.application_id)
    assert application is not None
    assert application.status == "EXPIRED_UNPAID"


async def test_a_paid_invoice_is_never_expired(db, paid_invoice):
    from app.modules.payments import jobs

    await jobs.expiry_sweep(db)
    await db.refresh(paid_invoice)
    assert paid_invoice.status == "paid"


async def test_the_sweep_is_idempotent(db, overdue_invoice):
    from sqlalchemy import func, select

    from app.modules.applications.models import ApplicationStatusHistory
    from app.modules.payments import jobs

    await jobs.expiry_sweep(db)
    await jobs.expiry_sweep(db)

    rows = await db.scalar(
        select(func.count())
        .select_from(ApplicationStatusHistory)
        .where(
            ApplicationStatusHistory.application_id == overdue_invoice.application_id,
            ApplicationStatusHistory.to_status == "EXPIRED_UNPAID",
        )
    )
    assert rows == 1


async def test_a_reminder_goes_out_before_the_deadline_once(db, invoice_due_in_two_days):
    from sqlalchemy import func, select

    from app.modules.notifications.models import Notification
    from app.modules.payments import jobs

    await jobs.expiry_sweep(db)
    await jobs.expiry_sweep(db)

    sent = await db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.object_id == invoice_due_in_two_days.id,
            Notification.event_code == "invoice.due_soon",
        )
    )
    assert sent == 1


async def test_every_event_this_module_notifies_on_has_a_template(db):
    from sqlalchemy import select

    from app.modules.notifications.models import NotificationTemplate
    from app.modules.payments import events

    for event_code in events.NOTIFIED_EVENT_CODES:
        row = await db.scalar(
            select(NotificationTemplate).where(
                NotificationTemplate.event_code == event_code,
                NotificationTemplate.channel == "inapp",
                NotificationTemplate.status == "active",
            )
        )
        assert row is not None, f"the payments migration seeds no template for {event_code}"


async def test_a_bad_transition_does_not_abort_the_sweep(
    db, non_invoiced_overdue_invoice, overdue_invoice
):
    """Ruling: the sweep skips and LOGS an invoice whose application cannot
    legally transition; it never lets `set_status` abort the whole run. In
    this shared, persistent test database a leftover committed overdue
    invoice from another test would otherwise make every payments test that
    runs after it fail — proven here by running both a malformed and a
    well-formed row through the SAME sweep call."""
    from app.modules.applications import service as applications_service
    from app.modules.payments import jobs

    await jobs.expiry_sweep(db)

    await db.refresh(non_invoiced_overdue_invoice)
    assert non_invoiced_overdue_invoice.status == "pending"  # untouched: refusal was caught
    stuck_application = await applications_service.get(
        db, non_invoiced_overdue_invoice.application_id
    )
    assert stuck_application is not None
    assert stuck_application.status == "APPROVED"  # never touched either

    await db.refresh(overdue_invoice)
    assert overdue_invoice.status == "expired"  # the well-formed row still processed
