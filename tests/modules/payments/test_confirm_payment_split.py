"""Task 5 of plan `07.9-payme-split` (decision #154): `confirm_payment`
writes one `allocations` row per receiver instead of the old fixed 50/50
pair — see `service.confirm_payment`'s own docstring for the full ruling.

The brief's four tests, verbatim, plus one pinning Override 4 (a legacy
invoice with no frozen snapshot at all).

Fixtures below build a 600 000 invoice split two ways:

- `paid_invoice_ctx` — the seeded `budget_50` (50%, migration `0045`) PLUS
  a second configured receiver, `receiver_fixed_60000` (a flat 60 000),
  positioned AFTER `budget_50` in `sort_order` so `issue_invoice`'s own
  `(sort_order, id)` read applies the percent rule first — 300 000 to
  `budget_50`, 60 000 to the fixed receiver, 240 000 left for the leshoz.
  Paid in FULL through the real `confirm_payment`, the same synthetic
  `provider="manual"` shape `test_refunds.py::_pay_in_full` uses.
- `invoice_600k` — the same application, only `budget_50` active, issued
  but left `pending` for the underpayment test to confirm itself.

`manual_confirm` (this file's own helper, named by the brief) is the
maker-checker door's shape without its machinery: a synthetic
`provider="manual"` `ProviderTransaction`, then `service.confirm_payment`
directly — this file tests THAT function, not the checker's HTTP route
(`backoffice_service._confirm_and_pay`, which builds the identical
transaction shape before calling the same function)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import NamedTuple

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization
from app.modules.applications import service as applications_service
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant
from app.modules.norms.models import Calculation
from app.modules.payments import repo, service
from app.modules.payments.models import Invoice, PaymentRecipient, ProviderTransaction
from tests.modules.applications.conftest import grazing_activity_id as grazing_activity_id
from tests.modules.gis.conftest import leshoz as leshoz
from tests.modules.payments.conftest import _new_approved_application


async def manual_confirm(
    db: AsyncSession, invoice: Invoice, *, amount: Decimal
) -> ProviderTransaction:
    """Synthesizes a `provider="manual"` `ProviderTransaction` for `amount`
    and calls `service.confirm_payment` directly — the same shape
    `backoffice_service._confirm_and_pay` builds for the checker's real
    HTTP route, without the maker-checker filing/approval machinery this
    file has no reason to also exercise. Returns the transaction so a
    caller that needs it back (`paid_invoice_ctx`, below) does not have to
    re-query for it."""
    transaction = ProviderTransaction(
        invoice_id=invoice.id,
        provider="manual",
        external_id=f"split-test-{uuid.uuid4().hex[:12]}",
        amount=amount,
        state="2",
        performed_at=datetime.now(UTC),
    )
    await repo.add_provider_transaction(db, transaction)
    await service.confirm_payment(db, invoice=invoice, transaction=transaction)
    return transaction


@pytest.fixture
async def receiver_fixed_60000(db: AsyncSession) -> PaymentRecipient:
    """A second configured receiver, `sort_order=20` — AFTER the seeded
    `budget_50`'s `10` — so `issue_invoice`'s `(sort_order, id)` read
    applies the percent rule before this fixed one, the order
    `test_a_payment_writes_one_allocation_per_receiver` pins."""
    row = PaymentRecipient(
        name={"uz_latn": "Ekologiya jamg'armasi"},
        kind="fixed",
        fixed_amount=Decimal("60000.00"),
        sort_order=20,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def application_600k(
    db: AsyncSession, applicant: Applicant, grazing_activity_id: uuid.UUID, leshoz: Organization
) -> Application:
    """An APPROVED application priced at 600 000 — round enough that a 50%
    rule and a flat 60 000 rule both land on a whole tiyin — pointed at
    `leshoz` (`assigned_org_id`, the same idiom `test_invoice_snapshot.py::
    leshoz_with_payme_id` uses) with a real bank account on its
    `requisites`, so `confirm_payment`'s recipient-account chain has
    something to resolve (`test_the_leshoz_row_carries_its_bank_account`)."""
    leshoz.requisites = {"account": "20208000900000000001"}
    row = await _new_approved_application(db, applicant)
    row.assigned_org_id = leshoz.id
    db.add(
        Calculation(
            application_id=row.id,
            activity_type_id=grazing_activity_id,
            rule_code_version="norms-1.0.0",
            input_snapshot={},
            amount=Decimal("600000.00"),
            breakdown={},
        )
    )
    await db.flush()
    return row


@pytest.fixture
async def invoice_600k(
    db: AsyncSession, application_600k: Application, budget_50: PaymentRecipient
) -> Invoice:
    """`application_600k`'s invoice, issued but left `pending` — snapshot
    frozen with only the seeded `budget_50` (50%) active, which
    `test_an_underpayment_is_split_over_what_arrived` confirms itself,
    against less than the invoice's own amount."""
    return await service.issue_invoice(db, application_600k.id)


class PaidInvoiceCtx(NamedTuple):
    """What `paid_invoice_ctx` hands the tests: the paid invoice and the
    transaction that paid it, the same pair `confirm_payment` itself takes."""

    invoice: Invoice
    transaction: ProviderTransaction


@pytest.fixture
async def paid_invoice_ctx(
    db: AsyncSession,
    application_600k: Application,
    budget_50: PaymentRecipient,
    receiver_fixed_60000: PaymentRecipient,
) -> PaidInvoiceCtx:
    """A 600 000 invoice, its split frozen at issuance over TWO configured
    receivers (`budget_50` 50%, `receiver_fixed_60000` a flat 60 000) plus
    the leshoz remainder, paid in FULL through the real `confirm_payment`."""
    invoice = await service.issue_invoice(db, application_600k.id)
    transaction = await manual_confirm(db, invoice, amount=invoice.amount)
    return PaidInvoiceCtx(invoice=invoice, transaction=transaction)


async def test_a_payment_writes_one_allocation_per_receiver(db, paid_invoice_ctx):
    entries = await service.allocations_for(db, paid_invoice_ctx.invoice.id)
    assert [(e.target, e.amount) for e in entries] == [
        ("receiver", Decimal("300000.00")),
        ("receiver", Decimal("60000.00")),
        ("recipient", Decimal("240000.00")),
    ]
    assert entries[-1].recipient_id is None


async def test_an_underpayment_is_split_over_what_arrived(db, invoice_600k, budget_50):
    # The manual maker-checker door: 400 000 confirmed against a 600 000 invoice.
    await manual_confirm(db, invoice_600k, amount=Decimal("400000.00"))
    entries = await service.allocations_for(db, invoice_600k.id)
    assert sum(e.amount for e in entries) == Decimal("400000.00")
    assert entries[0].amount == Decimal("200000.00")


async def test_the_ledger_totals_the_payment_exactly(db, paid_invoice_ctx):
    entries = await service.allocations_for(db, paid_invoice_ctx.invoice.id)
    assert sum(e.amount for e in entries) == paid_invoice_ctx.transaction.amount


async def test_the_leshoz_row_carries_its_bank_account(db, paid_invoice_ctx):
    entries = await service.allocations_for(db, paid_invoice_ctx.invoice.id)
    assert entries[-1].account == "20208000900000000001"


@pytest.fixture
async def legacy_invoice(db: AsyncSession, approved_application: Application) -> Invoice:
    """An invoice shaped like one issued BEFORE this stage: the application
    reached `INVOICED` and the invoice row exists, but nothing ever wrote an
    `invoice_recipients` snapshot for it. `issue_invoice` is the ONLY path
    that writes one, and every path through it TODAY (migration `0045`
    seeds `budget_50` active) writes at least the leshoz's own remainder
    row (`test_invoice_snapshot.py::test_an_empty_directory_gives_the_
    leshoz_the_whole_invoice`) — so this exact historical shape (an EMPTY
    snapshot, not a one-row one) can only be built directly, bypassing
    `issue_invoice` outright, the same way `conftest.py`'s own `invoice`
    fixture builds the row but — unlike that fixture — also carries the
    application to INVOICED so `confirm_payment`'s own `set_status(...,
    "PAID")` is a legal transition."""
    await applications_service.set_status(db, approved_application.id, to_status="INVOICED")
    row = Invoice(
        application_id=approved_application.id,
        number=f"INV-2027-{uuid.uuid4().hex[:6]}",
        amount=Decimal("100.00"),
        status="pending",
    )
    db.add(row)
    await db.flush()
    return row


async def test_an_invoice_with_no_frozen_snapshot_pays_the_leshoz_everything(
    db: AsyncSession, legacy_invoice: Invoice
):
    """Override 4: an invoice issued BEFORE this stage carries no
    `invoice_recipients` rows at all — `invoice_recipients` returns `[]`,
    not the ONE `kind='remainder'` row an invoice issued against an empty
    but ACTIVE directory would get. `ledger.split_payment` already reads an
    empty rule list as "everything to the leshoz" (Task 2) — this pins that
    `confirm_payment` actually reaches that reading via `[row for row in
    snapshot if row.kind != "remainder"]` on an EMPTY snapshot, rather than
    refusing an invoice with nothing to rebuild rules from."""
    snapshot = await service.invoice_recipients(db, legacy_invoice.id)
    assert snapshot == []

    await manual_confirm(db, legacy_invoice, amount=legacy_invoice.amount)

    entries = await service.allocations_for(db, legacy_invoice.id)
    assert len(entries) == 1
    assert entries[0].recipient_id is None
    assert entries[0].target == "recipient"
    assert entries[0].amount == legacy_invoice.amount
