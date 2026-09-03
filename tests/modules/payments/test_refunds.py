"""The manual refund (plan `03.10b-payments-reconciliation` task 9, `tz/08`,
decision #12). The pure half first (`core.time.add_working_days`,
`refunds.hint`, `refunds.breakdown_is_complete`), then the service/route
half: `POST /refunds`, `POST /refunds/{id}/submit-decision`,
`POST /refunds/{id}/approve`.

**A hint is never an error** (ruling 2, `backoffice_service._hint_for_invoice`'s
own docstring lists all six degenerate cases): every one of them still files
the refund, with `suggested_amount=None` and a non-empty `suggestion_reason`
instead of a raised exception. This file pins each one individually, not just
the two the brief's own worked examples name.

**The negative allocations land on `approve`, never on `submit-decision`**
(ruling 4) — `test_submit_decision_writes_no_allocations` pins the half of
that split a bare reading of the brief's own numbered tests would not catch.

`rf01` looks up the seeded `refund_reasons` item `RF-01` (migration `0022`)
by CODE rather than a hard-coded id — its own row id is `gen_random_uuid()`
at migration time, so no literal UUID for it is real across databases.

Every Payme-shaped id in this file is generated per run (the shared,
persistent test DB lesson — `test_reversal.py`'s own module docstring), and
every ledger assertion is scoped to the invoice THIS test created — an
unscoped query over `allocations` counts every other test's committed rows
too."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, publish
from app.core.time import add_working_days, business_today
from app.main import create_app
from app.modules.admin.models import ClassifierItem
from app.modules.applications.events import APPLICATION_APPROVED
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, User
from app.modules.norms.models import Calculation
from app.modules.payments import refunds
from app.modules.payments import repo as payments_repo
from app.modules.payments import service as payments_service
from app.modules.payments.models import Allocation, Invoice, ProviderTransaction
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.applications.conftest import published_contour as published_contour
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.gis.conftest import _commit_pending_before_requests
from tests.modules.gis.conftest import approval_doc as approval_doc
from tests.modules.gis.conftest import contours_layer as contours_layer
from tests.modules.gis.conftest import leshoz as leshoz

API = "/api/v1"
REFUNDS = f"{API}/refunds"


@pytest.fixture
async def rf01(db: AsyncSession) -> uuid.UUID:
    return (
        await db.execute(select(ClassifierItem.id).where(ClassifierItem.code == "RF-01"))
    ).scalar_one()


# --- pure tests: `add_working_days` ------------------------------------------


def test_add_working_days_skips_weekends_and_no_holidays():
    # Tue 2026-09-01 + 20 working days -> Tue 2026-09-29 (ruling 18: Mon-Fri only)
    assert add_working_days(date(2026, 9, 1), 20) == date(2026, 9, 29)


def test_add_working_days_skips_a_weekend_landing_in_the_middle():
    # Fri 2026-09-04 + 1 working day -> Mon 2026-09-07, not Sat
    assert add_working_days(date(2026, 9, 4), 1) == date(2026, 9, 7)


# --- pure tests: `refunds.hint` ----------------------------------------------


def test_the_hint_is_paid_times_the_unused_share_of_the_period():
    # a 100-day period, 40 days used, 60 unused (inclusive of `on_date`)
    assert refunds.hint(
        paid=Decimal("1000000.00"),
        period_from=date(2026, 1, 1),
        period_to=date(2026, 4, 10),
        on_date=date(2026, 2, 10),
    ) == Decimal("600000.00")


def test_a_period_already_over_hints_zero_not_a_negative_number():
    assert refunds.hint(
        paid=Decimal("1000000.00"),
        period_from=date(2026, 1, 1),
        period_to=date(2026, 1, 31),
        on_date=date(2026, 6, 1),
    ) == Decimal("0.00")


def test_a_request_before_the_period_starts_hints_the_whole_amount():
    assert refunds.hint(
        paid=Decimal("1000000.00"),
        period_from=date(2026, 5, 1),
        period_to=date(2026, 5, 31),
        on_date=date(2026, 4, 1),
    ) == Decimal("1000000.00")


# --- pure tests: `refunds.breakdown_is_complete` -----------------------------


def test_breakdown_is_complete_when_the_three_components_sum_to_final():
    assert refunds.breakdown_is_complete(
        Decimal("600000.00"), Decimal("100000.00"), Decimal("500000.00"), Decimal("0.00")
    )


def test_breakdown_is_complete_treats_a_missing_component_as_zero():
    assert refunds.breakdown_is_complete(Decimal("500000.00"), None, Decimal("500000.00"), None)


def test_breakdown_is_not_complete_when_the_sum_disagrees():
    assert not refunds.breakdown_is_complete(
        Decimal("600000.00"), Decimal("100000.00"), Decimal("400000.00"), Decimal("0.00")
    )


# --- fixtures: an application with a real period snapshot and a paid invoice -


@pytest.fixture
async def refund_application(
    db: AsyncSession,
    applicant: Applicant,
    grazing_activity_id: uuid.UUID,
    published_contour,
    leshoz,
) -> Application:
    """An APPROVED application bound to a real contour (so the ledger's
    recipient half resolves to a real account, mirroring
    `test_end_to_end.py::application_with_org_account`), with a Calculation
    whose `input_snapshot["request"]` carries a 100-day period straddling
    "today": 40 days already used, 60 unused — the exact worked example the
    brief pins."""
    leshoz.requisites = {"account": "20208000123456789012"}
    today = business_today()
    period_from = today - timedelta(days=40)
    period_to = today + timedelta(days=59)  # 40 + 59 + 1 = 100 days total
    application = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
        status="APPROVED",
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
    )
    db.add(application)
    await db.flush()
    db.add(
        Calculation(
            application_id=application.id,
            activity_type_id=grazing_activity_id,
            rule_code_version="norms-1.0.0",
            input_snapshot={
                "request": {
                    "period_from": period_from.isoformat(),
                    "period_to": period_to.isoformat(),
                }
            },
            amount=Decimal("1000000.00"),
            breakdown={},
        )
    )
    await db.flush()
    return application


async def _pay_in_full(db: AsyncSession, invoice: Invoice) -> ProviderTransaction:
    """Pays `invoice` in full through the REAL `confirm_payment` (never a
    hand-set `status='paid'`) — the same synthetic-transaction shape
    `backoffice_service._confirm_and_pay` feeds it, so the 50/50 ledger this
    file's own tests read back is the genuine one, not a stand-in."""
    transaction = ProviderTransaction(
        invoice_id=invoice.id,
        provider="manual",
        external_id=f"refund-test-{uuid.uuid4().hex[:12]}",
        amount=invoice.amount,
        state="2",
        performed_at=datetime.now(UTC),
    )
    await payments_repo.add_provider_transaction(db, transaction)
    await payments_service.confirm_payment(db, invoice=invoice, transaction=transaction)
    return transaction


@pytest.fixture
async def paid_refund_invoice(db: AsyncSession, refund_application: Application) -> Invoice:
    await publish(
        db, Event(name=APPLICATION_APPROVED, payload={"application_id": refund_application.id})
    )
    await db.commit()
    invoice = await payments_service.invoice_for_application(db, refund_application.id)
    assert invoice is not None
    await _pay_in_full(db, invoice)
    await db.commit()
    await db.refresh(invoice)
    return invoice


# --- HTTP clients -------------------------------------------------------------


@pytest.fixture
async def head(db: AsyncSession) -> User:
    """THE production checker: `executor_head` («Ваколатли шахс»), the one
    role migration 0022 grants `payments.confirm` to (mirrors
    `test_manual_confirmation.py::head`'s own reasoning)."""
    return await make_user(db, role_code="executor_head")


@pytest.fixture
async def head_client(db: AsyncSession, head: User):
    _, token, csrf = await make_session(db, head)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client


async def _request_refund(client, application_id: uuid.UUID, rf01: uuid.UUID) -> dict:
    response = await client.post(
        REFUNDS, json={"application_id": str(application_id), "basis_item_id": str(rf01)}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- integration tests --------------------------------------------------------


async def test_request_refund_files_with_the_frozen_calculations_hint(
    owner_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """Test 5: `POST /refunds` on a `paid` invoice creates `status=
    "requested"`, `suggested_amount` from the FROZEN calculation
    (`invoice.calculation_id`, never the newest one), and `due_at ==
    add_working_days(today, 20)`."""
    response = await owner_client.post(
        REFUNDS,
        json={
            "application_id": str(refund_application.id),
            "basis_item_id": str(rf01),
            "comment": "period cut short",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "requested"
    assert body["suggested_amount"] == "600000.00"
    assert body["suggestion_reason"] is None
    assert body["invoice_id"] == str(paid_refund_invoice.id)
    assert date.fromisoformat(body["due_at"]) == add_working_days(business_today(), 20)


async def test_a_stranger_applicant_cannot_request_a_refund_for_another_application(
    applicant_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """Ruling 7's ownership half: a stranger applicant gets the same 404 a
    missing application would (never 403 — the `get_invoice_for_actor`
    oracle reasoning)."""
    response = await applicant_client.post(
        REFUNDS,
        json={"application_id": str(refund_application.id), "basis_item_id": str(rf01)},
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "ERR-SYS-003"


async def test_an_accountant_may_file_a_refund_for_any_application(
    payments_view_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """Ruling 7's other half: `payments.manage` files for anyone, not only
    the invoice's own applicant."""
    response = await payments_view_client.post(
        REFUNDS,
        json={"application_id": str(refund_application.id), "basis_item_id": str(rf01)},
    )
    assert response.status_code == 201, response.text


async def test_an_invoice_with_no_calculation_still_files_the_refund(
    db: AsyncSession,
    payments_view_client,
    approved_application: Application,
    rf01: uuid.UUID,
):
    """Test 6: an invoice with `calculation_id IS NULL` — but otherwise
    `paid`, hence in-force — still files the refund, with `suggested_amount
    is None` and a non-empty `suggestion_reason`. A hint is never an
    error."""
    invoice = Invoice(
        application_id=approved_application.id,
        number=f"INV-2027-{uuid.uuid4().hex[:6]}",
        amount=Decimal("100.00"),
        status="paid",
        paid_at=datetime.now(UTC),
    )
    db.add(invoice)
    await db.commit()

    response = await payments_view_client.post(
        REFUNDS,
        json={"application_id": str(approved_application.id), "basis_item_id": str(rf01)},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["suggested_amount"] is None
    assert body["suggestion_reason"] == "calculation_missing"
    assert body["status"] == "requested"


async def test_no_in_force_invoice_still_files_the_refund_with_a_reason(
    payments_view_client,
    approved_application: Application,
    cancelled_invoice: Invoice,
    rf01: uuid.UUID,
):
    """Ruling 2's "no in-force invoice" case: the only invoice on record for
    this application is `cancelled` — `invoice_for_application` finds
    nothing in force, the fallback binds the refund to that same invoice for
    the FK, and the hint degrades rather than erroring."""
    response = await payments_view_client.post(
        REFUNDS,
        json={
            "application_id": str(cancelled_invoice.application_id),
            "basis_item_id": str(rf01),
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["suggested_amount"] is None
    assert body["suggestion_reason"] == "no_in_force_invoice"
    assert body["invoice_id"] == str(cancelled_invoice.id)


@pytest.mark.parametrize(
    ("input_snapshot", "expected_reason"),
    [
        pytest.param({}, "snapshot_missing_request", id="no_request_key"),
        pytest.param(
            {"request": {"period_from": "not-a-date", "period_to": "2026-01-01"}},
            "period_unparseable",
            id="unparseable_period",
        ),
        pytest.param(
            {"request": {"period_from": "2026-05-10", "period_to": "2026-05-01"}},
            "period_zero_length",
            id="zero_length_period",
        ),
    ],
)
async def test_a_malformed_frozen_snapshot_still_files_the_refund(
    db: AsyncSession,
    payments_view_client,
    approved_application: Application,
    grazing_activity_id: uuid.UUID,
    rf01: uuid.UUID,
    input_snapshot: dict,
    expected_reason: str,
):
    """Ruling 2's three degenerate-snapshot cases, parametrized — they differ
    only in `input_snapshot` and the `suggestion_reason` it degrades to: the
    frozen calculation's own `input_snapshot` has no `"request"` object at
    all (`approved_application`'s own fixture builds exactly this: `{}`), an
    unparseable date, and a `period_to` strictly before `period_from` (never
    fed to `refunds.hint`, whose own precondition is a positive-length
    period). A hint is never an error in any of the three."""
    calc = Calculation(
        application_id=approved_application.id,
        activity_type_id=grazing_activity_id,
        rule_code_version="norms-1.0.0",
        input_snapshot=input_snapshot,
        amount=Decimal("100.00"),
        breakdown={},
    )
    db.add(calc)
    await db.flush()
    invoice = Invoice(
        application_id=approved_application.id,
        calculation_id=calc.id,
        number=f"INV-2027-{uuid.uuid4().hex[:6]}",
        amount=Decimal("100.00"),
        status="paid",
        paid_at=datetime.now(UTC),
    )
    db.add(invoice)
    await db.commit()

    response = await payments_view_client.post(
        REFUNDS,
        json={"application_id": str(approved_application.id), "basis_item_id": str(rf01)},
    )
    assert response.status_code == 201, response.text
    assert response.json()["suggestion_reason"] == expected_reason


async def test_submit_decision_with_a_wrong_breakdown_answers_err_val_001(
    payments_view_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """Test 7: a breakdown that does not sum to `final_amount` answers
    `ERR-VAL-001` (422), checked in code before the database CHECK."""
    filed = await _request_refund(payments_view_client, refund_application.id, rf01)
    response = await payments_view_client.post(
        f"{REFUNDS}/{filed['id']}/submit-decision",
        json={
            "final_amount": "600000.00",
            "budget_amount": "100000.00",
            "recipient_amount": "400000.00",  # sums to 500000, not 600000
            "other_amount": "0.00",
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ERR-VAL-001"


async def test_submit_decision_requires_payments_manage(
    owner_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    filed = await _request_refund(owner_client, refund_application.id, rf01)
    response = await owner_client.post(
        f"{REFUNDS}/{filed['id']}/submit-decision",
        json={
            "final_amount": "600000.00",
            "budget_amount": "100000.00",
            "recipient_amount": "500000.00",
            "other_amount": "0.00",
        },
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "ERR-ACL-001"


async def test_submit_decision_writes_no_allocations(
    db: AsyncSession,
    payments_view_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """Ruling 4's own split, pinned directly: `submit-decision` stores the
    figures and moves the refund to `in_review` — it never touches
    `allocations`, and the invoice's ledger is unchanged."""
    filed = await _request_refund(payments_view_client, refund_application.id, rf01)
    before = (
        (
            await db.execute(
                select(Allocation).where(Allocation.invoice_id == paid_refund_invoice.id)
            )
        )
        .scalars()
        .all()
    )

    response = await payments_view_client.post(
        f"{REFUNDS}/{filed['id']}/submit-decision",
        json={
            "final_amount": "600000.00",
            "budget_amount": "100000.00",
            "recipient_amount": "500000.00",
            "other_amount": "0.00",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "in_review"

    after = (
        (
            await db.execute(
                select(Allocation).where(Allocation.invoice_id == paid_refund_invoice.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(after) == len(before)


async def test_approve_requires_payments_confirm(
    payments_view_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    filed = await _request_refund(payments_view_client, refund_application.id, rf01)
    await payments_view_client.post(
        f"{REFUNDS}/{filed['id']}/submit-decision",
        json={
            "final_amount": "600000.00",
            "budget_amount": "100000.00",
            "recipient_amount": "500000.00",
            "other_amount": "0.00",
        },
    )
    response = await payments_view_client.post(
        f"{REFUNDS}/{filed['id']}/approve", json={"resolution": "returned"}
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "ERR-ACL-001"


async def test_approve_writes_negative_allocations_and_the_ledger_sums_to_paid_minus_final(
    db: AsyncSession,
    payments_view_client,
    head_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """Tests 8, 9 and 10 together: `approve` with `resolution="returned"`
    writes one negative `entry_type="refund"` allocation per NON-ZERO
    component, each carrying `refund_id`; the invoice's WHOLE ledger sums to
    `paid - final_amount`; the budget component's allocation has
    `account is None` (`tz/12` #15, pinned again); and neither the invoice
    nor the application is moved."""
    filed = await _request_refund(payments_view_client, refund_application.id, rf01)
    await payments_view_client.post(
        f"{REFUNDS}/{filed['id']}/submit-decision",
        json={
            "final_amount": "600000.00",
            "budget_amount": "100000.00",
            "recipient_amount": "500000.00",
            "other_amount": "0.00",
        },
    )

    response = await head_client.post(
        f"{REFUNDS}/{filed['id']}/approve", json={"resolution": "returned"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "returned"

    # --- test 9: the budget allocation's account is NULL, deliberately, and
    # the response says so explicitly rather than omitting the field.
    assert body["budget_account"] is None
    assert body["recipient_account"] == "20208000123456789012"
    budget_entry = next(a for a in body["allocations"] if a["target"] == "budget")
    assert budget_entry["account"] is None
    assert budget_entry["amount"] == "-100000.00"
    recipient_entry = next(a for a in body["allocations"] if a["target"] == "recipient")
    assert recipient_entry["account"] == "20208000123456789012"
    assert recipient_entry["amount"] == "-500000.00"
    assert len(body["allocations"]) == 2  # `other_amount` is "0.00" — no row for it

    # --- test 8: every allocation on this invoice carries a `refund_id` for
    # the two just written, and the WHOLE ledger sums to paid - final.
    rows = (
        (
            await db.execute(
                select(Allocation).where(Allocation.invoice_id == paid_refund_invoice.id)
            )
        )
        .scalars()
        .all()
    )
    refund_rows = [r for r in rows if r.entry_type == "refund"]
    assert len(refund_rows) == 2
    assert all(r.refund_id == uuid.UUID(filed["id"]) for r in refund_rows)
    total = sum((r.amount for r in rows), Decimal("0.00"))
    assert total == paid_refund_invoice.amount - Decimal("600000.00")

    # --- test 10: neither the invoice nor the application moved.
    await db.refresh(paid_refund_invoice)
    assert paid_refund_invoice.status == "paid"
    application = await db.get(Application, refund_application.id)
    assert application is not None
    assert application.status == "PAID"


async def test_approve_refuses_a_refund_on_a_never_paid_invoice(
    db: AsyncSession,
    payments_view_client,
    head_client,
    pending_invoice: Invoice,
    rf01: uuid.UUID,
):
    """The finding's probe 1: `POST /refunds` on a `pending` (never-paid)
    invoice still files (ruling 2 — filing is not the door;
    `_hint_for_invoice` already answers `suggestion_reason ==
    "no_in_force_invoice"`), and `submit-decision` accepts any complete
    breakdown regardless of invoice status (ruling 4 — it stores figures,
    it never touches money). The refusal has to land on `approve`, the ONLY
    place `allocations` is written: `ERR-PAY-004` (409) — the invoice
    itself cannot be acted on in its CURRENT status, the same code
    `service.py`'s own state-conflict guards use — and no allocation is
    written, so the ledger stays `0.00`."""
    filed = await _request_refund(payments_view_client, pending_invoice.application_id, rf01)
    assert filed["suggestion_reason"] == "no_in_force_invoice"
    submitted = await payments_view_client.post(
        f"{REFUNDS}/{filed['id']}/submit-decision",
        json={
            "final_amount": "50000.00",
            "budget_amount": "10000.00",
            "recipient_amount": "40000.00",
            "other_amount": "0.00",
        },
    )
    assert submitted.status_code == 200, submitted.text

    response = await head_client.post(
        f"{REFUNDS}/{filed['id']}/approve", json={"resolution": "returned"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ERR-PAY-004"

    rows = (
        (await db.execute(select(Allocation).where(Allocation.invoice_id == pending_invoice.id)))
        .scalars()
        .all()
    )
    assert rows == []


async def test_approve_refuses_a_second_full_refund_on_the_same_invoice(
    db: AsyncSession,
    payments_view_client,
    head_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """The finding's probe 2: a first refund for the WHOLE invoice amount is
    approved (the ledger goes from `+1000000.00` to `0.00`), then a second
    refund is filed and approved on the same invoice. `invoice.status`
    stays `paid` across a refund (ruling 6 — neither branch touches it), so
    the status check from probe 1 does not fire here at all; it is the
    BALANCE the ledger itself carries — `0.00` after the first refund —
    that refuses the second. `ERR-VAL-001` (422), not `ERR-PAY-004`:
    the invoice's status is not in conflict, its arithmetic is."""
    first = await _request_refund(payments_view_client, refund_application.id, rf01)
    first_submit = await payments_view_client.post(
        f"{REFUNDS}/{first['id']}/submit-decision",
        json={
            "final_amount": "1000000.00",
            "budget_amount": "500000.00",
            "recipient_amount": "500000.00",
            "other_amount": "0.00",
        },
    )
    assert first_submit.status_code == 200, first_submit.text
    first_approve = await head_client.post(
        f"{REFUNDS}/{first['id']}/approve", json={"resolution": "returned"}
    )
    assert first_approve.status_code == 200, first_approve.text

    second = await _request_refund(payments_view_client, refund_application.id, rf01)
    second_submit = await payments_view_client.post(
        f"{REFUNDS}/{second['id']}/submit-decision",
        json={
            "final_amount": "600000.00",
            "budget_amount": "100000.00",
            "recipient_amount": "500000.00",
            "other_amount": "0.00",
        },
    )
    assert second_submit.status_code == 200, second_submit.text

    response = await head_client.post(
        f"{REFUNDS}/{second['id']}/approve", json={"resolution": "returned"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ERR-VAL-001"

    rows = (
        (
            await db.execute(
                select(Allocation).where(Allocation.invoice_id == paid_refund_invoice.id)
            )
        )
        .scalars()
        .all()
    )
    total = sum((r.amount for r in rows), Decimal("0.00"))
    assert total == Decimal("0.00")


async def test_approve_refuses_a_refund_after_a_post_perform_reversal(
    db: AsyncSession,
    payments_view_client,
    head_client,
    refund_application: Application,
    rf01: uuid.UUID,
):
    """The finding's probe 3: a Payme perform followed by a post-perform
    cancel (`service.record_reversal`, ruling 15) brings the ledger back to
    `0.00` WITHOUT moving `invoice.status` off `paid` (ruling 15's own
    deliberate choice — the invoice's history stays intact). A refund filed
    afterwards still gets a stale non-zero hint from `_hint_for_invoice`,
    which reads `invoice.status`/`invoice.amount` alone and knows nothing
    about the ledger — but `approve` refuses it anyway: the invoice IS
    `paid` (`ERR-PAY-004` does not fire), yet the ledger sums to `0.00`, so
    any positive `final_amount` fails the balance check (`ERR-VAL-001`)."""
    await publish(
        db, Event(name=APPLICATION_APPROVED, payload={"application_id": refund_application.id})
    )
    await db.commit()
    invoice = await payments_service.invoice_for_application(db, refund_application.id)
    assert invoice is not None
    transaction = await _pay_in_full(db, invoice)
    await db.commit()
    await db.refresh(invoice)

    await payments_service.record_reversal(db, invoice=invoice, transaction=transaction, reason=5)
    await db.commit()
    await db.refresh(invoice)
    assert invoice.status == "paid"  # ruling 15: never rewritten

    filed = await _request_refund(payments_view_client, refund_application.id, rf01)
    submitted = await payments_view_client.post(
        f"{REFUNDS}/{filed['id']}/submit-decision",
        json={
            "final_amount": "600000.00",
            "budget_amount": "100000.00",
            "recipient_amount": "500000.00",
            "other_amount": "0.00",
        },
    )
    assert submitted.status_code == 200, submitted.text

    response = await head_client.post(
        f"{REFUNDS}/{filed['id']}/approve", json={"resolution": "returned"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ERR-VAL-001"

    rows = (
        (await db.execute(select(Allocation).where(Allocation.invoice_id == invoice.id)))
        .scalars()
        .all()
    )
    total = sum((r.amount for r in rows), Decimal("0.00"))
    assert total == Decimal("0.00")


async def test_two_partial_refunds_within_the_remaining_balance_both_succeed(
    db: AsyncSession,
    payments_view_client,
    head_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """The balance guard is not "forbid refunds" wearing a disguise: two
    PARTIAL refunds against the same 1,000,000.00 invoice both succeed as
    long as each stays within what the ledger still carries —
    400,000.00 first, leaving 600,000.00, then a SECOND refund for exactly
    that remaining 600,000.00 (`final_amount == balance`, the boundary —
    pinned here rather than as a case that is merely "under" the limit)."""
    first = await _request_refund(payments_view_client, refund_application.id, rf01)
    first_submit = await payments_view_client.post(
        f"{REFUNDS}/{first['id']}/submit-decision",
        json={
            "final_amount": "400000.00",
            "budget_amount": "100000.00",
            "recipient_amount": "300000.00",
            "other_amount": "0.00",
        },
    )
    assert first_submit.status_code == 200, first_submit.text
    first_approve = await head_client.post(
        f"{REFUNDS}/{first['id']}/approve", json={"resolution": "returned"}
    )
    assert first_approve.status_code == 200, first_approve.text

    second = await _request_refund(payments_view_client, refund_application.id, rf01)
    second_submit = await payments_view_client.post(
        f"{REFUNDS}/{second['id']}/submit-decision",
        json={
            "final_amount": "600000.00",
            "budget_amount": "100000.00",
            "recipient_amount": "500000.00",
            "other_amount": "0.00",
        },
    )
    assert second_submit.status_code == 200, second_submit.text
    second_approve = await head_client.post(
        f"{REFUNDS}/{second['id']}/approve", json={"resolution": "returned"}
    )
    assert second_approve.status_code == 200, second_approve.text

    rows = (
        (
            await db.execute(
                select(Allocation).where(Allocation.invoice_id == paid_refund_invoice.id)
            )
        )
        .scalars()
        .all()
    )
    total = sum((r.amount for r in rows), Decimal("0.00"))
    assert total == Decimal("0.00")


async def test_approve_rejected_writes_no_allocations(
    db: AsyncSession,
    payments_view_client,
    head_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """`resolution="rejected"` moves the refund to `rejected` and writes
    NOTHING to `allocations` — no money moved, so there is nothing to
    reverse."""
    filed = await _request_refund(payments_view_client, refund_application.id, rf01)
    await payments_view_client.post(
        f"{REFUNDS}/{filed['id']}/submit-decision",
        json={
            "final_amount": "600000.00",
            "budget_amount": "100000.00",
            "recipient_amount": "500000.00",
            "other_amount": "0.00",
        },
    )
    before = (
        (
            await db.execute(
                select(Allocation).where(Allocation.invoice_id == paid_refund_invoice.id)
            )
        )
        .scalars()
        .all()
    )

    response = await head_client.post(
        f"{REFUNDS}/{filed['id']}/approve", json={"resolution": "rejected", "comment": "no basis"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rejected"
    assert body["budget_account"] is None
    assert body["allocations"] == []

    after = (
        (
            await db.execute(
                select(Allocation).where(Allocation.invoice_id == paid_refund_invoice.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(after) == len(before)
