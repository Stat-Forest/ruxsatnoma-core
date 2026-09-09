"""The maker-checker manual PAID (plan `03.10b-payments-reconciliation`
tasks 6 and 7) — `tz/08` §4's one exception to `tz/05` invariant 3.

Two halves of one mechanism. The accountant (`payments.manage`) FILES a
confirmation backed by a real bank document and nothing moves; the leshoz
head (`payments.confirm`, granted to `executor_head` by migration 0022)
CHECKS it, and only that approval turns the invoice `paid` — through
`payments.service.confirm_payment`, the very function the Payme webhook
calls, never a second money path of this file's own.

Two things this file deliberately pins rather than fixes:

- **a configured receiver's `account` is always NULL, the leshoz's is not**
  (Override 5, stage 7.9 task 5 — a `payment_recipients` row identifies a
  PAYME WALLET, not a bank account, so this is permanent by design, not an
  open question waiting on the Agency; `tz/12` #15, the state budget's OWN
  account number, was reformulated the same day decision #159 landed and no
  longer blocks this path at all — Payme routes by recipient id, not a
  stored account number).
- **the `payment_confirmed` bus hop reaches `permits`** exactly as the Payme
  path does (`tests/test_cross_module_journey.py` hop 3). A manual PAID that
  does not tell the executor a permit is due is PR #31's defect with a
  different door, and no single module's suite can see it.
"""

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import MediaFile
from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.audit.models import AuditLog
from app.modules.auth.models import User
from app.modules.notifications.models import Notification
from app.modules.payments import backoffice_service
from app.modules.payments import events as payment_events
from app.modules.payments import service as payments_service
from app.modules.payments.models import (
    Allocation,
    Invoice,
    ManualPaymentConfirmation,
    PaymentRecipient,
    ProviderTransaction,
    Reconciliation,
)
from app.modules.permits import events as permit_events
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.gis.conftest import _commit_pending_before_requests

# `pending_invoice_with_org_account` (and the whole fixture chain under it)
# is the ONLY application fixture in this package whose leshoz carries a real
# bank account in its `requisites` — without it `_resolve_recipient_account`
# short-circuits to `None` and the `tz/12` #15 test below would pass for the
# wrong reason (both halves NULL). pytest resolves a fixture's own parameters
# against the CURRENT test's closure, not the file it was defined in, so the
# four primitives underneath it need re-exporting here too (the same chain
# `test_end_to_end.py` itself had to re-export).
from tests.modules.payments.test_end_to_end import (
    application_with_org_account as application_with_org_account,
)
from tests.modules.payments.test_end_to_end import approval_doc as approval_doc
from tests.modules.payments.test_end_to_end import contours_layer as contours_layer
from tests.modules.payments.test_end_to_end import leshoz as leshoz
from tests.modules.payments.test_end_to_end import (
    pending_invoice_with_org_account as pending_invoice_with_org_account,
)
from tests.modules.payments.test_end_to_end import published_contour as published_contour

API = "/api/v1"
MANUAL_CONFIRMATIONS = f"{API}/payments/manual-confirmations"


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
async def bank_doc(db: AsyncSession) -> MediaFile:
    """A genuine `media_files` row: `bank_doc_file_id` is a NOT NULL FK, and
    ruling 1 makes the document what makes the exception legal."""
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


async def _client_for(db: AsyncSession, user: User) -> AsyncIterator[httpx.AsyncClient]:
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client


@pytest.fixture
async def head(db: AsyncSession) -> User:
    """THE production checker: `executor_head` («Ваколатли шахс», the leshoz
    head), the one role migration 0022 grants `payments.confirm` to — never
    an actor with the code bolted on as a personal `user_permissions` row,
    which would prove the CODE works and say nothing about the role
    (`conftest.py::payments_view_client`'s own reasoning)."""
    return await make_user(db, role_code="executor_head")


@pytest.fixture
async def head_client(db: AsyncSession, head: User) -> AsyncIterator[httpx.AsyncClient]:
    async for client in _client_for(db, head):
        yield client


@pytest.fixture
async def second_head(db: AsyncSession) -> User:
    """A SECOND `executor_head` — the independent checker for the case where
    the first one is the maker."""
    return await make_user(db, role_code="executor_head")


@pytest.fixture
async def second_head_client(
    db: AsyncSession, second_head: User
) -> AsyncIterator[httpx.AsyncClient]:
    async for client in _client_for(db, second_head):
        yield client


async def _file_via_http(
    client: httpx.AsyncClient,
    invoice: Invoice,
    bank_doc: MediaFile,
    *,
    amount: Decimal | None = None,
) -> dict:
    response = await client.post(
        MANUAL_CONFIRMATIONS,
        json={
            "invoice_id": str(invoice.id),
            "amount": str(invoice.amount if amount is None else amount),
            "paid_at": "2026-09-01T10:00:00Z",
            "bank_doc_file_id": str(bank_doc.id),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _allocations(db: AsyncSession, invoice_id: uuid.UUID) -> list[Allocation]:
    return list(
        await db.scalars(
            select(Allocation).where(Allocation.invoice_id == invoice_id).order_by(Allocation.id)
        )
    )


# --- Task 6: the maker --------------------------------------------------------


async def test_a_manual_confirmation_without_a_bank_document_is_refused(
    payments_view_client, invoice: Invoice
):
    """Ruling 1 / tz/08 §4: the document is what makes the exception legal."""
    response = await payments_view_client.post(
        MANUAL_CONFIRMATIONS,
        json={
            "invoice_id": str(invoice.id),
            "amount": "2060000.00",
            "paid_at": "2026-09-01T10:00:00Z",
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ERR-VAL-001"


async def test_the_bank_document_must_name_an_active_media_file(
    payments_view_client, db: AsyncSession, invoice: Invoice, bank_doc: MediaFile
):
    """An EXISTENCE check, not a validity one — an archived row is as good as
    no document at all."""
    bank_doc.status = "archived"
    await db.flush()

    response = await payments_view_client.post(
        MANUAL_CONFIRMATIONS,
        json={
            "invoice_id": str(invoice.id),
            "amount": str(invoice.amount),
            "paid_at": "2026-09-01T10:00:00Z",
            "bank_doc_file_id": str(bank_doc.id),
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["reason"] == "bank_doc_not_active"


async def test_filing_leaves_the_invoice_untouched(
    payments_view_client, db: AsyncSession, pending_invoice: Invoice, bank_doc: MediaFile
):
    """Filing is not paying (ruling 4): the row is `pending_check`, the
    invoice is still `pending` with no `paid_at`, and nothing was allocated."""
    body = await _file_via_http(payments_view_client, pending_invoice, bank_doc)

    assert body["status"] == "pending_check"
    assert body["checker_id"] is None
    assert body["amount_matches_invoice"] is True

    await db.refresh(pending_invoice)
    assert pending_invoice.status == "pending"
    assert pending_invoice.paid_at is None
    assert await _allocations(db, pending_invoice.id) == []


async def test_an_applicant_may_not_file_a_manual_confirmation(
    applicant_client, invoice: Invoice, bank_doc: MediaFile
):
    response = await applicant_client.post(
        MANUAL_CONFIRMATIONS,
        json={
            "invoice_id": str(invoice.id),
            "amount": str(invoice.amount),
            "paid_at": "2026-09-01T10:00:00Z",
            "bank_doc_file_id": str(bank_doc.id),
        },
    )
    assert response.status_code == 403, response.text


async def test_a_second_confirmation_while_one_is_pending_answers_err_pay_004(
    payments_view_client, pending_invoice: Invoice, bank_doc: MediaFile
):
    await _file_via_http(payments_view_client, pending_invoice, bank_doc)

    second = await payments_view_client.post(
        MANUAL_CONFIRMATIONS,
        json={
            "invoice_id": str(pending_invoice.id),
            "amount": str(pending_invoice.amount),
            "paid_at": "2026-09-02T10:00:00Z",
            "bank_doc_file_id": str(bank_doc.id),
        },
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "ERR-PAY-004"


async def test_a_cancelled_invoice_may_not_gain_money_by_this_door(
    payments_view_client, cancelled_invoice: Invoice, bank_doc: MediaFile
):
    """`tz/05` invariant 3 read backwards: an `expired`/`cancelled` invoice
    must not become payable because an accountant filed a document."""
    response = await payments_view_client.post(
        MANUAL_CONFIRMATIONS,
        json={
            "invoice_id": str(cancelled_invoice.id),
            "amount": str(cancelled_invoice.amount),
            "paid_at": "2026-09-01T10:00:00Z",
            "bank_doc_file_id": str(bank_doc.id),
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ERR-PAY-004"


async def test_an_underpayment_is_accepted_recorded_and_flagged(
    payments_view_client, db: AsyncSession, pending_invoice: Invoice, bank_doc: MediaFile
):
    """Ruling 5: an underpayment is a real thing an accountant confirms and
    then reconciles — accepted and recorded, never refused, but flagged both
    on the response and as an OPEN row in the discrepancy register."""
    short = pending_invoice.amount - Decimal("500.00")
    body = await _file_via_http(payments_view_client, pending_invoice, bank_doc, amount=short)

    assert body["amount_matches_invoice"] is False
    assert Decimal(body["amount"]) == short

    row = (
        await db.scalars(
            select(Reconciliation).where(Reconciliation.invoice_id == pending_invoice.id)
        )
    ).one()
    assert row.status == "open"
    assert row.result == "discrepancy"
    # `difference` is paid MINUS invoiced on every row that COMPARES a
    # payment with an invoice (`matcher.py`'s own convention, also this
    # module's) — an underpayment is negative. `service.record_reversal`
    # is the one row that does NOT compare: it stores the money that went
    # back, positive (`service.py`'s own docstring at that call site).
    assert row.difference == Decimal("-500.00")


# --- Task 7: the checker ------------------------------------------------------


async def test_the_maker_may_not_be_the_checker(
    head_client, db: AsyncSession, head: User, pending_invoice: Invoice, bank_doc: MediaFile
):
    """Ruling 13, checked in the SERVICE and before any write — so the caller
    reads a clean 403 rather than an IntegrityError 500 from the
    `confirmed_needs_checker` database CHECK, and the confirmation is left
    exactly as it was."""
    filed = await backoffice_service.file_manual_confirmation(
        db,
        invoice_id=pending_invoice.id,
        amount=pending_invoice.amount,
        paid_at=datetime.now(UTC),
        bank_doc_file_id=bank_doc.id,
        actor=head,
    )
    confirmation_id = filed.confirmation.id
    await db.commit()

    response = await head_client.post(f"{MANUAL_CONFIRMATIONS}/{confirmation_id}/confirm")
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "ERR-ACL-001"

    row = await db.get(ManualPaymentConfirmation, confirmation_id, populate_existing=True)
    assert row is not None
    assert row.status == "pending_check"
    assert row.checker_id is None
    await db.refresh(pending_invoice)
    assert pending_invoice.status == "pending"


async def test_an_accountant_may_not_confirm(
    payments_view_client, pending_invoice: Invoice, bank_doc: MediaFile
):
    """The maker files; only `payments.confirm` checks. The accountant holds
    `payments.view`/`payments.manage` and neither of those is it."""
    body = await _file_via_http(payments_view_client, pending_invoice, bank_doc)

    response = await payments_view_client.post(f"{MANUAL_CONFIRMATIONS}/{body['id']}/confirm")
    assert response.status_code == 403, response.text


async def test_a_confirmation_pays_the_invoice_through_the_one_existing_path(
    payments_view_client,
    head_client,
    db: AsyncSession,
    pending_invoice: Invoice,
    bank_doc: MediaFile,
):
    """Invoice `paid`, application `PAID`, exactly two allocations summing to
    the transaction amount, and ONE synthetic `provider_transactions` row —
    `provider="manual"`, `state="2"`, `external_id == str(confirmation.id)`
    (ruling 14). No second ledger path exists."""
    filed = await _file_via_http(payments_view_client, pending_invoice, bank_doc)
    confirmation_id = uuid.UUID(filed["id"])

    response = await head_client.post(f"{MANUAL_CONFIRMATIONS}/{confirmation_id}/confirm")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert response.json()["checker_id"] is not None

    await db.refresh(pending_invoice)
    assert pending_invoice.status == "paid"
    assert pending_invoice.paid_at is not None

    application = await db.get(Application, pending_invoice.application_id, populate_existing=True)
    assert application is not None
    assert application.status == "PAID"

    transactions = list(
        await db.scalars(
            select(ProviderTransaction).where(ProviderTransaction.invoice_id == pending_invoice.id)
        )
    )
    assert len(transactions) == 1
    transaction = transactions[0]
    assert transaction.provider == "manual"
    assert transaction.state == "2"
    assert transaction.external_id == str(confirmation_id)
    assert transaction.amount == pending_invoice.amount

    allocations = await _allocations(db, pending_invoice.id)
    assert len(allocations) == 2
    # Stage 7.9 task 5: the seeded `budget_50` directory row (migration
    # `0045`) is now a configured RECEIVER, not the old engine's fixed
    # "budget" half.
    assert {a.target for a in allocations} == {"recipient", "receiver"}
    assert sum(a.amount for a in allocations) == transaction.amount


async def test_ri_01_is_written_to_the_audit_journal_as_a_success(
    payments_view_client,
    head_client,
    db: AsyncSession,
    pending_invoice: Invoice,
    bank_doc: MediaFile,
):
    """`tz/10`: «PAID без подтверждения провайдера/банка». The action
    SUCCEEDED and is legal — the indicator says a human should look, which is
    the opposite of 3.11a's RI-10 denial, so `result` is `"success"` and the
    row lives in the payment's own transaction, not an early commit."""
    filed = await _file_via_http(payments_view_client, pending_invoice, bank_doc)
    confirmation_id = uuid.UUID(filed["id"])

    response = await head_client.post(f"{MANUAL_CONFIRMATIONS}/{confirmation_id}/confirm")
    assert response.status_code == 200, response.text

    rows = list(
        await db.scalars(
            select(AuditLog).where(
                AuditLog.action == backoffice_service.MANUAL_CONFIRM_ACTION,
                AuditLog.object_id == pending_invoice.id,
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].extra is not None
    assert rows[0].extra["risk_indicator"] == "RI-01"
    assert rows[0].result == "success"


async def test_the_leshoz_has_a_bank_account_and_the_configured_receiver_never_does(
    payments_view_client,
    head_client,
    db: AsyncSession,
    pending_invoice_with_org_account: Invoice,
    bank_doc: MediaFile,
):
    """Review round 1, Minor 3 (renamed — the old name and docstring
    described the pre-7.9 fixed 50/50 engine's untracked state-budget
    account, `tz/12` #15; the body now asserts something else entirely).
    Override 5, stage 7.9 task 5: a `payment_recipients` row identifies a
    PAYME WALLET, not a bank account, so a configured receiver's own
    `allocations.account` always stays `None` — by design, permanently, not
    because nobody has answered `tz/12` #15 yet. The leshoz's account still
    comes off its own organization `requisites`, exactly as before."""
    invoice = pending_invoice_with_org_account
    filed = await _file_via_http(payments_view_client, invoice, bank_doc)

    response = await head_client.post(f"{MANUAL_CONFIRMATIONS}/{filed['id']}/confirm")
    assert response.status_code == 200, response.text

    by_target = {a.target: a for a in await _allocations(db, invoice.id)}
    assert by_target["recipient"].account == "20208000123456789012"
    # Stage 7.9 task 5: `budget_50` is now a configured RECEIVER, and a
    # receiver's own account always stays `None` (Override 5).
    assert by_target["receiver"].account is None


async def test_the_payment_confirmed_event_reaches_permits(
    payments_view_client,
    head_client,
    db: AsyncSession,
    approved_application: Application,
    pending_invoice: Invoice,
    bank_doc: MediaFile,
):
    """The seam PR #31 exists because of, asserted for the manual door:
    `permits.subscribers.on_payment_confirmed` must hear this the same way it
    hears a Payme `PerformTransaction` (`tests/test_cross_module_journey.py`
    hop 3). Without an assignee that subscriber returns early and notifies
    nobody, so this test gives the application one."""
    executor = await make_user(db, role_code="executor_staff")
    approved_application.assigned_user_id = executor.id
    await db.flush()

    filed = await _file_via_http(payments_view_client, pending_invoice, bank_doc)
    response = await head_client.post(f"{MANUAL_CONFIRMATIONS}/{filed['id']}/confirm")
    assert response.status_code == 200, response.text

    due = list(
        await db.scalars(
            select(Notification).where(
                Notification.event_code == permit_events.PERMIT_DUE,
                Notification.recipient_user_id == executor.id,
                Notification.object_id == approved_application.id,
            )
        )
    )
    assert due, (
        "the bus hop payment_confirmed -> permits.on_payment_confirmed did not happen "
        "for a manual PAID"
    )


async def test_the_maker_is_told_their_confirmation_was_approved(
    payments_view_client,
    head_client,
    db: AsyncSession,
    approved_application: Application,
    pending_invoice: Invoice,
    bank_doc: MediaFile,
):
    """`payment.manual_confirmed` goes to the MAKER, never the applicant
    (coordinator ruling): `confirm_payment` has already told the applicant on
    `payment.confirmed`, on both `inapp` and `sms`, so sending them this one
    too was two billed Cyrillic SMS for one payment. The applicant does not
    care by which door their payment was confirmed; the accountant who filed
    it does, and nothing else tells them it was approved."""
    filed = await _file_via_http(payments_view_client, pending_invoice, bank_doc)
    maker_id = uuid.UUID(filed["maker_id"])

    response = await head_client.post(f"{MANUAL_CONFIRMATIONS}/{filed['id']}/confirm")
    assert response.status_code == 200, response.text

    recipients = set(
        await db.scalars(
            select(Notification.recipient_user_id).where(
                Notification.event_code == payment_events.PAYMENT_MANUAL_CONFIRMED,
                Notification.object_id == pending_invoice.id,
            )
        )
    )
    assert recipients == {maker_id}
    # And the applicant was told exactly once, by `confirm_payment`'s own
    # `payment.confirmed` — the double-notification this ruling removed.
    assert approved_application.submitted_by_user_id not in recipients


async def test_confirming_an_already_checked_confirmation_answers_err_pay_004(
    payments_view_client,
    head_client,
    second_head_client,
    pending_invoice: Invoice,
    bank_doc: MediaFile,
):
    filed = await _file_via_http(payments_view_client, pending_invoice, bank_doc)

    first = await head_client.post(f"{MANUAL_CONFIRMATIONS}/{filed['id']}/confirm")
    assert first.status_code == 200, first.text

    second = await second_head_client.post(f"{MANUAL_CONFIRMATIONS}/{filed['id']}/confirm")
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "ERR-PAY-004"


# --- reject -------------------------------------------------------------------


async def test_reject_refuses_an_empty_reason(
    payments_view_client, head_client, pending_invoice: Invoice, bank_doc: MediaFile
):
    """A missing field is FastAPI's own validation; a present-but-blank one is
    the service's check, since a Pydantic `str` requirement cannot see past
    whitespace the way `str.strip()` can. Both are `ERR-VAL-001`."""
    filed = await _file_via_http(payments_view_client, pending_invoice, bank_doc)

    missing = await head_client.post(f"{MANUAL_CONFIRMATIONS}/{filed['id']}/reject", json={})
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["code"] == "ERR-VAL-001"

    blank = await head_client.post(
        f"{MANUAL_CONFIRMATIONS}/{filed['id']}/reject", json={"reason": "   "}
    )
    assert blank.status_code == 422, blank.text
    assert blank.json()["error"]["details"]["reason"] == "reason_required"


async def test_reject_records_the_reason_and_moves_no_money(
    payments_view_client,
    head_client,
    db: AsyncSession,
    pending_invoice: Invoice,
    bank_doc: MediaFile,
):
    """`rejected` with a reason; the invoice stays `pending`, the ledger stays
    empty, no `provider_transactions` row is synthesized, and RI-01 — which
    fires when an invoice actually becomes PAID — is not raised."""
    filed = await _file_via_http(payments_view_client, pending_invoice, bank_doc)

    response = await head_client.post(
        f"{MANUAL_CONFIRMATIONS}/{filed['id']}/reject",
        json={"reason": "the payment order names a different invoice"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rejected"
    assert body["reason"] == "the payment order names a different invoice"
    assert body["checker_id"] is not None

    await db.refresh(pending_invoice)
    assert pending_invoice.status == "pending"
    assert pending_invoice.paid_at is None
    assert await _allocations(db, pending_invoice.id) == []
    assert (
        list(
            await db.scalars(
                select(ProviderTransaction).where(
                    ProviderTransaction.invoice_id == pending_invoice.id
                )
            )
        )
        == []
    )

    ri_rows = list(
        await db.scalars(
            select(AuditLog).where(
                AuditLog.action == backoffice_service.MANUAL_CONFIRM_ACTION,
                AuditLog.object_id == pending_invoice.id,
            )
        )
    )
    assert ri_rows == []


async def test_a_rejected_invoice_can_be_confirmed_by_a_fresh_filing(
    payments_view_client,
    head_client,
    second_head_client,
    db: AsyncSession,
    pending_invoice: Invoice,
    bank_doc: MediaFile,
):
    """A rejected confirmation must not block the invoice forever — the
    "one pending_check at a time" guard is about a PENDING row, not a
    terminal one."""
    first = await _file_via_http(payments_view_client, pending_invoice, bank_doc)
    rejected = await head_client.post(
        f"{MANUAL_CONFIRMATIONS}/{first['id']}/reject", json={"reason": "wrong document"}
    )
    assert rejected.status_code == 200, rejected.text

    second = await _file_via_http(payments_view_client, pending_invoice, bank_doc)
    confirmed = await second_head_client.post(f"{MANUAL_CONFIRMATIONS}/{second['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text

    await db.refresh(pending_invoice)
    assert pending_invoice.status == "paid"


# --- the amount bound (critical, review round 1) ------------------------------
#
# The Payme door pins the amount to `invoice.amount`
# (`CreateTransaction`/`CheckPerformTransaction` refuse a mismatch with
# `-31001`). This door removed that guard and, for one round, put nothing in
# its place: `-2060000.00` was driven end to end against a real 150 000,00
# invoice — filed 201, confirmed 200, invoice `paid`, application `PAID`, two
# NEGATIVE allocations. `0.00` settled the invoice in full with a zero ledger.
# Ruling 5 accepts an UNDERPAYMENT, never a negative or a zero settlement.


@pytest.mark.parametrize(
    ("amount", "case"),
    [
        ("0.00", "a zero settlement is not a payment"),
        ("-2060000.00", "a negative amount would write a negative ledger"),
        ("-0.01", "one tiyin below zero is still below zero"),
        # 19 integer digits — `invoices.amount` and `allocations.amount` are
        # `numeric(18, 2)`, so this must be a 422 at the edge, never a
        # `DataError` 500 out of the database.
        ("1234567890123456789.00", "wider than numeric(18, 2) can hold"),
    ],
)
async def test_an_amount_that_is_not_money_is_refused(
    payments_view_client,
    db: AsyncSession,
    pending_invoice: Invoice,
    bank_doc: MediaFile,
    amount: str,
    case: str,
):
    response = await payments_view_client.post(
        MANUAL_CONFIRMATIONS,
        json={
            "invoice_id": str(pending_invoice.id),
            "amount": amount,
            "paid_at": "2026-09-01T10:00:00Z",
            "bank_doc_file_id": str(bank_doc.id),
        },
    )
    assert response.status_code == 422, f"{case}: {response.text}"
    assert response.json()["error"]["code"] == "ERR-VAL-001"

    # Refused at the edge means nothing at all was written.
    assert (
        list(
            await db.scalars(
                select(ManualPaymentConfirmation).where(
                    ManualPaymentConfirmation.invoice_id == pending_invoice.id
                )
            )
        )
        == []
    )
    await db.refresh(pending_invoice)
    assert pending_invoice.status == "pending"


async def test_an_overpayment_is_still_accepted(
    payments_view_client, db: AsyncSession, pending_invoice: Invoice, bank_doc: MediaFile
):
    """No business ceiling, deliberately: an overpayment is a documented
    refund ground in `tz/08`, so the bound that refuses zero and negatives
    must not also refuse a real case. Flagged like any other mismatch."""
    over = pending_invoice.amount + Decimal("1000.00")
    body = await _file_via_http(payments_view_client, pending_invoice, bank_doc, amount=over)

    assert body["amount_matches_invoice"] is False
    row = (
        await db.scalars(
            select(Reconciliation).where(Reconciliation.invoice_id == pending_invoice.id)
        )
    ).one()
    assert row.difference == Decimal("1000.00")


# --- the split does not fit (review round 1, Important 1) ---------------------
#
# `ManualConfirmationIn.amount` is bounded only `gt=0` (see the section
# above) — an accountant may confirm LESS than the invoice, the whole point
# of the underpayment path. But `confirm_payment` re-runs the frozen split
# against whatever amount was confirmed, and a configured FIXED receiver
# larger than that amount makes the split not fit (`ledger.SplitDoesNotFit`).
# Before this fix that exception was unhandled here and reached the HTTP
# layer as a bare 500; the controller ruling is REFUSE, mirroring
# `issue_invoice` — see `service.confirm_payment`'s own docstring.


@pytest.fixture
async def receiver_fixed_50000(db: AsyncSession) -> PaymentRecipient:
    """A configured FIXED receiver, 50 000 — chosen so that, ALONGSIDE the
    seeded `budget_50` (50%), it still fits `approved_application`'s full
    150 000.00 invoice at issuance (75 000 + 50 000 = 125 000, leaving the
    leshoz 25 000), but no longer fits the 40 000
    `test_a_split_that_does_not_fit_is_refused_through_the_http_route`
    confirms — a manual confirmation smaller than this fixed amount, the
    combination review round 1's Important 1 names."""
    row = PaymentRecipient(
        name={"uz_latn": "Ekologiya jamg'armasi"},
        kind="fixed",
        fixed_amount=Decimal("50000.00"),
        sort_order=20,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def pending_invoice_with_fixed_receiver(
    db: AsyncSession,
    approved_application: Application,
    receiver_fixed_50000: PaymentRecipient,
) -> Invoice:
    """`approved_application`'s invoice (150 000.00), issued through the
    real `service.issue_invoice` — application -> INVOICED, invoice left
    `pending` — with `receiver_fixed_50000` already flushed so
    `issue_invoice`'s own directory read freezes it onto the split. A
    fixture parameter, not a row created inside this fixture's own body:
    `issue_invoice` reads `payment_recipients` directly and exactly once,
    so the receiver must exist BEFORE this call, the same ordering
    `test_confirm_payment_split.py::paid_invoice_ctx` relies on."""
    return await payments_service.issue_invoice(db, approved_application.id)


async def test_a_split_that_does_not_fit_is_refused_through_the_http_route(
    payments_view_client,
    head_client,
    db: AsyncSession,
    pending_invoice_with_fixed_receiver: Invoice,
    bank_doc: MediaFile,
):
    """Checked through the REAL maker-checker HTTP route — filed by the
    accountant, confirmed by the head — not just `service.confirm_payment`
    called directly: `app.main`'s global `DomainError` handler must turn
    the refusal into a proper `ERR-VAL-001` response (422), never a bare
    500, and nothing may be written on the way there."""
    invoice = pending_invoice_with_fixed_receiver
    filed = await _file_via_http(
        payments_view_client, invoice, bank_doc, amount=Decimal("40000.00")
    )

    response = await head_client.post(f"{MANUAL_CONFIRMATIONS}/{filed['id']}/confirm")

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "ERR-VAL-001"
    assert body["error"]["details"]["reason"] == "split_does_not_fit"

    assert await _allocations(db, invoice.id) == []
    await db.refresh(invoice)
    assert invoice.status == "pending"
    assert invoice.paid_at is None


# --- sys_admin is a superuser for permissions, not for maker-checker ----------


async def test_a_sys_admin_may_not_check_its_own_filing(
    db: AsyncSession, pending_invoice: Invoice, bank_doc: MediaFile
):
    """`require_permission` lets a `sys_admin` past every permission code as a
    superuser — so it reaches BOTH routes, and it is the one actor that can
    file and then check with no second grant. Whose two pairs of eyes saw this
    money is not a permission question, so `check_manual_confirmation` refuses
    it anyway, in the service, before any write.

    Driven entirely through HTTP precisely because the superuser bypass lives
    in the dependency: a service-level call would never exercise it, and this
    is exactly the property a future refactor of `require_permission` would
    silently break."""
    admin = await make_user(db, role_code="sys_admin")
    async for client in _client_for(db, admin):
        filed = await _file_via_http(client, pending_invoice, bank_doc)
        assert uuid.UUID(filed["maker_id"]) == admin.id

        response = await client.post(f"{MANUAL_CONFIRMATIONS}/{filed['id']}/confirm")
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "ERR-ACL-001"

        # A reject is one person deciding alone just as much as a confirm is.
        rejected = await client.post(
            f"{MANUAL_CONFIRMATIONS}/{filed['id']}/reject",
            json={"reason": "on second thoughts"},
        )
        assert rejected.status_code == 403, rejected.text

        # Inside the generator's own body: `filed` is bound only if the loop
        # ran, and pyright is right to refuse to assume it did.
        row = await db.get(
            ManualPaymentConfirmation, uuid.UUID(filed["id"]), populate_existing=True
        )
        assert row is not None
        assert row.status == "pending_check"
        assert row.checker_id is None
        await db.refresh(pending_invoice)
        assert pending_invoice.status == "pending"


# --- Task defect 4b: a pending confirmation was undiscoverable ---------------
#
# No route listed pending manual confirmations, so the maker handed the
# invoice id to the checker by hand — the payments-side twin of gis's own
# undiscoverable contour version (task defect 4a), fixed as one pattern:
# `GET /payments/manual-confirmations`, permission-gated on either role that
# can act on a filing, zone-scoped like every other list.


async def test_the_checker_finds_a_pending_filing_without_being_handed_the_id(
    payments_view_client, head_client, pending_invoice: Invoice, bank_doc: MediaFile
):
    filed = await _file_via_http(payments_view_client, pending_invoice, bank_doc)

    # Default `?status=` is `pending_check` — the checker's own worklist.
    listed = await head_client.get(MANUAL_CONFIRMATIONS)
    assert listed.status_code == 200, listed.text
    ids = {item["id"] for item in listed.json()["items"]}
    assert filed["id"] in ids

    confirmed = await head_client.post(f"{MANUAL_CONFIRMATIONS}/{filed['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text

    # Confirmed, it drops off the default (pending) worklist and shows under
    # its own status instead — never silently disappearing.
    after = await head_client.get(MANUAL_CONFIRMATIONS)
    assert filed["id"] not in {item["id"] for item in after.json()["items"]}
    resolved = await head_client.get(f"{MANUAL_CONFIRMATIONS}?status=confirmed")
    assert filed["id"] in {item["id"] for item in resolved.json()["items"]}


async def test_the_maker_can_also_list_their_own_filing(
    payments_view_client, pending_invoice: Invoice, bank_doc: MediaFile
):
    """Both roles that can act on a filing need to find it (mirrors gis's
    `require_any_permission(CONTOURS_MANAGE, CONTOURS_APPROVE)`)."""
    filed = await _file_via_http(payments_view_client, pending_invoice, bank_doc)
    listed = await payments_view_client.get(MANUAL_CONFIRMATIONS)
    assert filed["id"] in {item["id"] for item in listed.json()["items"]}


async def test_a_stranger_with_neither_role_is_refused(applicant_client):
    response = await applicant_client.get(MANUAL_CONFIRMATIONS)
    assert response.status_code == 403, response.text


async def test_manual_confirmations_are_zone_scoped(
    db: AsyncSession, bank_doc: MediaFile, applicant
):
    """A filing whose invoice's application belongs to one leshoz stays
    invisible to a maker/checker zoned to a DIFFERENT one, exactly the shape
    `test_invoice_zone.py` proves for reading and paying the invoice itself
    (decision #70). Built locally rather than importing that file's fixtures:
    this needs BOTH an `accountant` (maker) and an `executor_head` (checker)
    zoned to the SAME leshoz, which that file has no reason to define."""
    from contextlib import asynccontextmanager

    from app.db import uuid7
    from tests.modules.payments.conftest import _new_approved_application

    async def _leshoz(label: str) -> Organization:
        existing_agency = (
            await db.execute(select(Organization).where(Organization.kind == "agency"))
        ).scalar_one_or_none()
        agency = existing_agency
        if agency is None:
            agency = Organization(
                id=uuid7(),
                code=f"A{uuid.uuid4().hex[:8]}",
                kind="agency",
                name={"uz_cyrl": "Агентлик", "uz_latn": "Agentlik"},
            )
            db.add(agency)
            await db.flush()
        org = Organization(
            id=uuid7(),
            parent_id=agency.id,
            code=f"L{uuid.uuid4().hex[:8]}",
            kind="leshoz",
            name={"uz_cyrl": label, "uz_latn": label},
        )
        db.add(org)
        await db.flush()
        return org

    @asynccontextmanager
    async def _role_client_in(role_code: str, org: Organization):
        user = await make_user(db, role_code=role_code, organization_id=org.id)
        _, token, csrf = await make_session(db, user)
        await db.commit()
        async with make_client(create_app(), lifespan=True) as client:
            auth_client(client, token, csrf)
            _commit_pending_before_requests(client, db)
            yield client

    home = await _leshoz("Burchmulla-4b")
    away = await _leshoz("Chimyon-4b")

    application = await _new_approved_application(db, applicant)
    application.assigned_org_id = home.id
    await db.flush()
    home_invoice = Invoice(
        application_id=application.id,
        number=f"INV-2027-{uuid.uuid4().hex[:6]}",
        amount=Decimal("100.00"),
        status="pending",
    )
    db.add(home_invoice)
    await db.flush()
    await db.commit()

    async with _role_client_in("accountant", home) as home_maker:
        filed = await _file_via_http(home_maker, home_invoice, bank_doc)

    async with _role_client_in("executor_head", away) as away_checker:
        away_listed = await away_checker.get(MANUAL_CONFIRMATIONS)
        assert filed["id"] not in {item["id"] for item in away_listed.json()["items"]}

    async with _role_client_in("executor_head", home) as home_checker:
        home_listed = await home_checker.get(MANUAL_CONFIRMATIONS)
        assert filed["id"] in {item["id"] for item in home_listed.json()["items"]}


# --- the checker's own read (stage 7.3, finding F13) -------------------------
#
# The defect these pin: the checker could list the confirmations awaiting them
# (`GET /payments/manual-confirmations`, 200) and could open NEITHER the invoice
# the confirmation is about (404) NOR the register (403 `payments.view`). So the
# second pair of eyes in a four-eyes control was asked to approve a payment
# without being able to see the invoice it pays, the application behind it, or
# the bank document that is the whole of the evidence. Measured on dev during
# the С1–С27 walkthrough, as `demo_executor_head`, on their own leshoz's invoice.


async def test_the_checker_opens_the_invoice_they_are_asked_to_confirm(
    head_client: httpx.AsyncClient, pending_invoice: Invoice
):
    result = await head_client.get(f"{API}/invoices/{pending_invoice.id}")
    assert result.status_code == 200, result.text
    assert result.json()["id"] == str(pending_invoice.id)


async def test_the_checker_browses_the_invoice_register(
    head_client: httpx.AsyncClient, pending_invoice: Invoice
):
    result = await head_client.get(f"{API}/invoices")
    assert result.status_code == 200, result.text
    assert str(pending_invoice.id) in {row["id"] for row in result.json()["items"]}


async def test_the_checkers_read_does_not_become_a_write(
    head_client: httpx.AsyncClient, pending_invoice: Invoice
):
    """`payments.confirm` buys the checker a READ of what they confirm and
    nothing else: raising a payment link is still `payments.view`'s, and the
    refusal stays the 404 a stranger gets rather than a 403 that would confirm
    the invoice exists."""
    result = await head_client.post(
        f"{API}/invoices/{pending_invoice.id}/pay-intents",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"provider": "payme"},
    )
    assert result.status_code == 404, result.text


# --- the prosecutor reads the money too (decision #95, tz/12 #48) ------------
#
# The stage 7.3 walkthrough measured `demo_prosecutor` reading applications and
# permits fine and getting `403 ERR-ACL-001 {"permission":"payments.view"}` on
# `GET /payments/allocations`. С22 lists «платежи (инвойсы, транзакции,
# распределение, сверка, refund)» among what the prosecutor inspects, so half of
# what oversight exists to look at was invisible to it.
#
# Built under the PRODUCTION `prosecutor` role, never a client with the code
# bolted on: what is under test is the role's own grants, which is exactly what
# a `user_permissions` shortcut would stop proving (lesson).


@pytest.fixture
async def prosecutor_client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    user = await make_user(db, role_code="prosecutor")
    async for client in _client_for(db, user):
        yield client


async def test_the_prosecutor_reads_the_invoice_register(
    prosecutor_client: httpx.AsyncClient, pending_invoice: Invoice
):
    result = await prosecutor_client.get(f"{API}/invoices")
    assert result.status_code == 200, result.text
    assert str(pending_invoice.id) in {row["id"] for row in result.json()["items"]}


async def test_the_prosecutor_reads_one_invoice_and_its_allocations(
    prosecutor_client: httpx.AsyncClient, pending_invoice: Invoice
):
    card = await prosecutor_client.get(f"{API}/invoices/{pending_invoice.id}")
    assert card.status_code == 200, card.text
    ledger = await prosecutor_client.get(
        f"{API}/payments/allocations", params={"invoice_id": str(pending_invoice.id)}
    )
    assert ledger.status_code == 200, ledger.text


async def test_the_prosecutor_still_writes_nothing(
    prosecutor_client: httpx.AsyncClient, pending_invoice: Invoice, bank_doc: uuid.UUID
):
    """С22: «Попытка write → 403». Reading everything must not become doing
    anything — this is the half of the ruling that a "grant the role more" change
    can quietly lose."""
    filed = await prosecutor_client.post(
        MANUAL_CONFIRMATIONS,
        json={
            "invoice_id": str(pending_invoice.id),
            "amount": "100.00",
            "paid_at": "2026-09-06T05:00:00Z",
            "bank_doc_file_id": str(bank_doc),
        },
    )
    assert filed.status_code == 403, filed.text
    assert filed.json()["error"]["code"] == "ERR-ACL-001"
