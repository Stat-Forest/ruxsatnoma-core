"""The invoice, issued by the `application_approved` subscriber (task 2).

The brief's own six tests, translated per ruling P4: `applications` has no
router.py/schemas.py on this branch (3.9a branch 2's `stage-3.9a-flow`, not
merged here), so no test drives `POST /applications/{id}/approve` or
`/cancel` — an `APPROVED` row is built directly through the ORM
(`approved_application`) and the two events this stage subscribes to are
published on the bus directly, exactly what the parallel branch's `approve()`
and `cancel()` will do once merged. Plus authorization tests for the two read
routes this task adds (`GET /invoices/{id}`, `GET /invoices?application_id=`)
— they are new and unguarded would be a hole."""

from datetime import timedelta

import pytest

API = "/api/v1"


async def test_approving_an_application_issues_its_invoice(db, approved_application):
    """The whole point of the event bus: 3.9a knows nothing about payments."""
    from app.core import events
    from app.modules.applications import events as app_events
    from app.modules.applications import service as applications_service
    from app.modules.payments import service

    calculation = await applications_service.current_calculation(db, approved_application.id)
    assert calculation is not None

    await events.publish(
        db,
        events.Event(
            name=app_events.APPLICATION_APPROVED,
            payload={"application_id": approved_application.id},
        ),
    )

    invoice = await service.invoice_for_application(db, approved_application.id)
    assert invoice is not None
    assert invoice.amount == calculation.amount, "ruling 7: the frozen calculation, not a re-price"
    assert invoice.status == "pending"
    assert invoice.number.startswith("INV-")
    assert (invoice.due_at - invoice.issued_at) == timedelta(days=10)


async def test_the_application_moves_to_invoiced(db, approved_application):
    from app.core import events
    from app.modules.applications import events as app_events
    from app.modules.applications import service as applications_service

    await events.publish(
        db,
        events.Event(
            name=app_events.APPLICATION_APPROVED,
            payload={"application_id": approved_application.id},
        ),
    )

    application = await applications_service.get(db, approved_application.id)
    assert application is not None
    assert application.status == "INVOICED"


async def test_the_invoice_records_which_calculation_it_billed(db, approved_application):
    """Ruling 17: 3.9b's recalculate writes a NEW calculation row, and its own
    ruling 17 forbids that after approval — but that guard lives on another
    branch. This column makes a divergence a two-column comparison instead of
    an invisible drift."""
    from app.core import events
    from app.modules.applications import events as app_events
    from app.modules.applications import service as applications_service
    from app.modules.payments import service

    await events.publish(
        db,
        events.Event(
            name=app_events.APPLICATION_APPROVED,
            payload={"application_id": approved_application.id},
        ),
    )

    invoice = await service.invoice_for_application(db, approved_application.id)
    calculation = await applications_service.current_calculation(db, approved_application.id)

    assert invoice is not None
    assert calculation is not None
    assert invoice.calculation_id == calculation.id
    assert invoice.amount == calculation.amount


async def test_cancelling_an_application_cancels_its_invoice(db, approved_application):
    """Ruling 16, half 1. Without this the checkout link keeps working after a
    withdrawal, and paying it drives PerformTransaction into an illegal
    CANCELLED -> PAID move inside a route that must always answer 200.

    Ruling P4: `POST /applications/{id}/cancel` does not exist on this
    branch — publish `APPLICATION_CANCELLED` directly, exactly what the
    parallel branch's `cancel()` will do."""
    from app.core import events
    from app.modules.applications import events as app_events
    from app.modules.payments import service
    from app.modules.payments.models import Invoice

    await events.publish(
        db,
        events.Event(
            name=app_events.APPLICATION_APPROVED,
            payload={"application_id": approved_application.id},
        ),
    )
    issued = await service.invoice_for_application(db, approved_application.id)
    assert issued is not None
    invoice_id = issued.id

    await events.publish(
        db,
        events.Event(
            name=app_events.APPLICATION_CANCELLED,
            payload={"application_id": approved_application.id},
        ),
    )

    assert await service.invoice_for_application(db, approved_application.id) is None
    cancelled = await db.get(Invoice, invoice_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"


async def test_cancelling_an_application_never_cancels_a_PAID_invoice(db, approved_application):
    """Whole-branch review: `invoice_for_application` returns `pending` OR
    `paid` (`repo.IN_FORCE_STATUSES`), so the cancellation handler is handed
    a settled invoice just as readily as an unpaid one. Cancelling that one
    destroys a confirmed payment — `is_paid` flips back to `False`, the
    ledger rows are orphaned against a cancelled invoice, and the citizen
    who paid gets neither a permit nor a refund record.

    Unreachable through `APPLICATION_TRANSITIONS` today (`PAID` may only go
    to `PERMIT_ISSUED`), but that guard lives in ANOTHER module and 3.9a-flow
    already writes one transition outside `set_status` — so the refusal is
    asserted on the handler itself, by publishing the event directly at a
    paid invoice, which is exactly what a future `cancel()` would do."""
    from app.core import events
    from app.modules.applications import events as app_events
    from app.modules.payments import service

    await events.publish(
        db,
        events.Event(
            name=app_events.APPLICATION_APPROVED,
            payload={"application_id": approved_application.id},
        ),
    )
    issued = await service.invoice_for_application(db, approved_application.id)
    assert issued is not None
    issued.status = "paid"
    await db.flush()

    await events.publish(
        db,
        events.Event(
            name=app_events.APPLICATION_CANCELLED,
            payload={"application_id": approved_application.id},
        ),
    )

    await db.refresh(issued)
    assert issued.status == "paid", "a confirmed payment's invoice is never cancelled here"
    assert await service.is_paid(db, approved_application.id) is True


async def test_a_repeated_event_returns_the_same_invoice_and_does_not_raise(
    db, approved_application
):
    """Ruling 11: the event fires inside the approval's transaction, so a retry
    re-fires it. Relying on IntegrityError here would turn a harmless retry into
    a failed approval in the applicant's face."""
    from sqlalchemy import func, select

    from app.core import events
    from app.modules.applications import events as app_events
    from app.modules.payments.models import Invoice

    payload = {"application_id": str(approved_application.id)}
    for _ in range(2):
        await events.publish(
            db, events.Event(name=app_events.APPLICATION_APPROVED, payload=payload)
        )

    count = await db.scalar(
        select(func.count())
        .select_from(Invoice)
        .where(Invoice.application_id == approved_application.id)
    )
    assert count == 1


async def test_an_application_with_no_calculation_fails_loudly(db, approved_without_calculation):
    """A silent zero-amount invoice is the failure mode this must not have.

    And the refusal must SAY SO. `issue_invoice` has no HTTP route of its own —
    it runs as a bus subscriber inside the publisher's transaction — so 3.9's
    `POST /applications/{id}/approve` is what answers the reviewer. It answered
    `ERR-SYS-003`, 404 «Ресурс не найден», until 2026-09-03: the approval rolled
    back and the reviewer could not tell that from "no such application". Same
    code and same reason as `permits.service.issue`'s identical refusal, so one
    condition has one representation on both sides of the seam.
    """
    from app.core.errors import DomainError
    from app.modules.payments import service

    with pytest.raises(DomainError) as excinfo:
        await service.issue_invoice(db, approved_without_calculation.id)

    assert excinfo.value.code == "ERR-VAL-001"
    assert excinfo.value.http_status == 422, "a 404 here reads as 'application not found'"
    assert excinfo.value.details is not None
    assert excinfo.value.details["reason"] == "no_calculation"


# --- read-route authorization -------------------------------------------------
# `GET /invoices/{id}` and `GET /invoices?application_id=` are new routes this
# task ships; an accountant (`payments.view`) sees any invoice, the invoice's
# own applicant sees it, and everyone else gets 404 — never 403, which would
# confirm to a stranger that the invoice exists.


@pytest.fixture
async def issued_invoice(db, approved_application):
    """`approved_application`'s invoice, issued through the real event path —
    what every authorization test below needs, without repeating the
    publish-and-fetch dance six times."""
    from app.core import events
    from app.modules.applications import events as app_events
    from app.modules.payments import service

    await events.publish(
        db,
        events.Event(
            name=app_events.APPLICATION_APPROVED,
            payload={"application_id": approved_application.id},
        ),
    )
    invoice = await service.invoice_for_application(db, approved_application.id)
    assert invoice is not None
    return invoice


async def test_owner_can_read_their_own_invoice(db, issued_invoice, owner_client):
    r = await owner_client.get(f"{API}/invoices/{issued_invoice.id}")
    assert r.status_code == 200
    assert r.json()["id"] == str(issued_invoice.id)


async def test_a_stranger_reading_an_invoice_gets_404_not_403(db, issued_invoice, applicant_client):
    r = await applicant_client.get(f"{API}/invoices/{issued_invoice.id}")
    assert r.status_code == 404


async def test_a_payments_view_holder_can_read_any_invoice(
    db, issued_invoice, payments_view_client
):
    r = await payments_view_client.get(f"{API}/invoices/{issued_invoice.id}")
    assert r.status_code == 200
    assert r.json()["id"] == str(issued_invoice.id)


async def test_owner_can_list_invoices_for_their_own_application(
    db, approved_application, issued_invoice, owner_client
):
    r = await owner_client.get(
        f"{API}/invoices", params={"application_id": str(approved_application.id)}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(issued_invoice.id)


async def test_a_stranger_listing_someone_elses_invoices_gets_404(
    db, approved_application, issued_invoice, applicant_client
):
    r = await applicant_client.get(
        f"{API}/invoices", params={"application_id": str(approved_application.id)}
    )
    assert r.status_code == 404


async def test_a_payments_view_holder_can_list_invoices_for_any_application(
    db, approved_application, issued_invoice, payments_view_client
):
    r = await payments_view_client.get(
        f"{API}/invoices", params={"application_id": str(approved_application.id)}
    )
    assert r.status_code == 200
    assert r.json()["total"] == 1
