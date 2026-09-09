"""Task 4 of plan `07.9-payme-split`: `issue_invoice` freezes the directory's
split onto the new invoice (decision #158), and `service.invoice_recipients`
reads it back.

The brief's five tests, translated per the CONTROLLER OVERRIDES in the
task's own brief:

- **Override 1** — `payment_recipients` is NOT empty in a fresh test
  database (migration `0045` seeds the state budget at 50%, active). The
  brief's last test therefore takes `budget_50_inactive` (`conftest.py`,
  Task 3) as an explicit fixture parameter, deactivating the seeded row for
  the duration of the test, rather than assuming an empty table.
- **Override 3** — `test_the_leshoz_payme_id_is_frozen_too` depends on BOTH
  `approved_application` and `leshoz_with_payme_id` but reads only
  `approved_application.id`: `leshoz_with_payme_id` (this file's own
  fixture, below) points `approved_application` itself at a leshoz carrying
  a Payme account id, as a SIDE EFFECT, the same idiom
  `test_manual_confirmation.py`'s own zone test uses for `assigned_org_id`.

`approved_application_cheap` and `fund_fixed_50000` (this file's own
fixtures, not in the shared `conftest.py`) build
`test_fixed_amounts_exceeding_the_invoice_refuse_to_issue_it`'s own
precondition: an invoice small enough that ONE configured fixed amount
already exceeds it, regardless of the seeded budget row's own 50%."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant
from app.modules.norms.models import Calculation
from app.modules.payments import service
from app.modules.payments.models import PaymentRecipient
from tests.modules.applications.conftest import grazing_activity_id as grazing_activity_id
from tests.modules.gis.conftest import leshoz as leshoz
from tests.modules.payments.conftest import _new_approved_application


@pytest.fixture
async def approved_application_cheap(
    db: AsyncSession, applicant: Applicant, grazing_activity_id: uuid.UUID
) -> Application:
    """An APPROVED application priced cheap enough that `fund_fixed_50000`
    ALONE already exceeds it — `test_fixed_amounts_exceeding_the_invoice_
    refuse_to_issue_it`'s own precondition. Mirrors `conftest.py`'s own
    `approved_application`, just with a smaller `Calculation.amount`."""
    row = await _new_approved_application(db, applicant)
    db.add(
        Calculation(
            application_id=row.id,
            activity_type_id=grazing_activity_id,
            rule_code_version="norms-1.0.0",
            input_snapshot={},
            amount=Decimal("100.00"),
            breakdown={},
        )
    )
    await db.flush()
    return row


@pytest.fixture
async def fund_fixed_50000(db: AsyncSession) -> PaymentRecipient:
    """A configured receiver whose fixed amount alone (50 000) already
    exceeds `approved_application_cheap`'s own invoice amount (100.00) —
    `ledger.split_payment` must refuse before `issue_invoice` ever writes a
    row. Built directly via `db`, never committed: the `db` fixture rolls
    back at teardown, so this needs none of `test_recipients_api.py`'s own
    `_reset_payment_recipients` cleanup, which exists only for rows that
    fixture's tests commit through the HTTP client."""
    row = PaymentRecipient(
        name={"uz_latn": "Maqsadli jamg'arma"},
        kind="fixed",
        fixed_amount=Decimal("50000.00"),
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def leshoz_with_payme_id(
    db: AsyncSession, approved_application: Application, leshoz: Organization
) -> Organization:
    """Points `approved_application` at `leshoz` (`gis/conftest.py`'s shared
    organization fixture — agency-or-reuse already handled there) after
    giving it a Payme account id — `resolve_leshoz_payme_id`'s own chain
    (`assigned_org_id` -> `admin.repo.get_organization` ->
    `requisites["payme_account_id"]`), exercised through `assigned_org_id`
    alone since `approved_application` names no contour at all (no GIS
    machinery needed, unlike `test_end_to_end.py::application_with_org_
    account`, which exercises the SIBLING chain's `contour_id` half).

    Mutates the ALREADY-BUILT `approved_application` row directly — the
    same idiom `test_manual_confirmation.py::test_manual_confirmations_are_
    zone_scoped` uses for `assigned_org_id` — rather than building a third
    application fixture: the test that depends on this one names
    `approved_application` itself, never this fixture's own return value."""
    leshoz.requisites = {"payme_account_id": "12345"}
    approved_application.assigned_org_id = leshoz.id
    await db.flush()
    return leshoz


async def test_issuing_an_invoice_freezes_the_directory(db, approved_application, budget_50):
    invoice = await service.issue_invoice(db, approved_application.id)
    rows = await service.invoice_recipients(db, invoice.id)
    assert [(r.kind, r.amount) for r in rows] == [
        ("percent", invoice.amount / 2),
        ("remainder", invoice.amount / 2),
    ]
    assert rows[-1].recipient_id is None


async def test_editing_the_directory_afterwards_changes_nothing(
    db, approved_application, budget_50
):
    invoice = await service.issue_invoice(db, approved_application.id)
    before = [(r.recipient_id, r.amount) for r in await service.invoice_recipients(db, invoice.id)]
    budget_50.percent = Decimal("10.00")
    await db.flush()
    after = [(r.recipient_id, r.amount) for r in await service.invoice_recipients(db, invoice.id)]
    assert after == before


async def test_the_leshoz_payme_id_is_frozen_too(db, approved_application, leshoz_with_payme_id):
    invoice = await service.issue_invoice(db, approved_application.id)
    rows = await service.invoice_recipients(db, invoice.id)
    assert rows[-1].payme_account_id == "12345"


async def test_fixed_amounts_exceeding_the_invoice_refuse_to_issue_it(
    db, approved_application_cheap, fund_fixed_50000
):
    with pytest.raises(DomainError) as excinfo:
        await service.issue_invoice(db, approved_application_cheap.id)
    assert excinfo.value.code == "ERR-VAL-001"
    assert excinfo.value.details is not None
    assert excinfo.value.details["reason"] == "split_does_not_fit"


async def test_an_empty_directory_gives_the_leshoz_the_whole_invoice(
    db, approved_application, budget_50_inactive
):
    invoice = await service.issue_invoice(db, approved_application.id)
    rows = await service.invoice_recipients(db, invoice.id)
    assert len(rows) == 1
    assert rows[0].amount == invoice.amount
