"""Ruling #185 (stage 10, track B4): a zero-sum invoice from a benefit
settles itself; a zero from anywhere else is still a refusal. Ruling #202
widens "from a benefit" to "lawfully zero": a statutory exemption
(`science`, priced by the calculator as `no_tariff_by_law` under the
versioned `tariff_exempt:science` parameter) settles the same way, and a
configured FIXED-amount receiver no longer refuses either zero at issuance.

Every test here walks the REAL path (plan B4 rule 3): `norms.calculator.
calculate` builds the `Calculation.breakdown` a genuine benefit claim would
produce — never hand-rolled JSON — and `APPLICATION_APPROVED` is PUBLISHED
on the real bus (`core.events.publish`), the same event
`applications.decision.approve` fires, picked up by the REAL subscriber
registration `tests/conftest.py::_isolate_subscriptions` wires into every
test (`payments.subscribers.on_application_approved` -> `service.
issue_invoice`). `permits.subscribers.on_payment_confirmed`'s own effect —
notifying the assigned executor that a permit is due — is observed through
its own stored `Notification` row, never through a mock: `_settle_free`
publishes `payment_confirmed` with the identical payload `confirm_payment`
does, and that subscriber must not be able to tell the two apart.
"""

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, publish
from app.core.models import MediaFile
from app.db import make_session_factory
from app.modules.applications import service as applications_service
from app.modules.applications.events import APPLICATION_APPROVED
from app.modules.applications.models import Application
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Applicant, User
from app.modules.norms.calculator import CalcRequest, ParamSnapshot, TariffFact, calculate
from app.modules.norms.models import Calculation
from app.modules.notifications.models import Notification
from app.modules.payments import events as payment_events
from app.modules.payments import service as payments_service
from app.modules.payments.models import (
    Allocation,
    InvoiceRecipient,
    PaymentRecipient,
    ProviderTransaction,
)
from app.modules.permits import events as permit_events
from tests.modules.auth.test_sessions import make_user
from tests.modules.norms.conftest import science_activity_id as science_activity_id

API = "/api/v1"

# A REAL calculator run, matching what decision #181 seeds: every benefit
# category carries a `"0"` modifier, so an approved claim prices at exactly
# zero. Only `bhm`/`rounding_money` are read for a flat (non-grazing)
# activity with no norm on record (`calculator.calculate`'s own gating).
_PARAMS = {"bhm": "412000", "rounding_money": {"mode": "half_up", "step": 1}}


def _free_calculation_fields(benefit_code: str = "war_veterans") -> tuple[Decimal, list[dict]]:
    """The shape a REAL `beekeeping_union_member`/`war_veterans`/... claim
    produces: `_apply_benefit` multiplies the tariff's own coefficient by
    the claimed modifier (`"0"`) and records a `{"kind": "benefit", ...}`
    breakdown line beside the `{"kind": "tariff", ...}` one — reused here
    rather than hand-rolled, per the plan's own instruction."""
    request = CalcRequest(
        activity_code="recreation",
        on_date=date(2026, 9, 10),
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("1"),
        items=(),
        quantity=Decimal("2"),
        benefit_code=benefit_code,
    )
    snapshot = ParamSnapshot(
        values=_PARAMS,
        tariffs=(
            TariffFact(
                id=None,
                livestock_group=None,
                coefficient=Decimal("50"),
                quantity_unit="person",
                benefit_modifiers={benefit_code: "0"},
            ),
        ),
        norm=None,
        load_sb=Decimal("0"),
        load_source="none",
    )
    result = calculate(request, snapshot)
    assert result.amount == Decimal("0")
    assert any(line["kind"] == "benefit" for line in result.breakdown)
    return result.amount, result.breakdown


def _plain_zero_calculation_fields() -> tuple[Decimal, list[dict]]:
    """Ruling #185's OTHER zero — a tariff row published at a genuine zero
    coefficient, no benefit claimed at all. The breakdown this produces
    carries no `benefit` line, which is exactly the fail-closed condition
    `issue_invoice` must keep refusing to settle for free."""
    request = CalcRequest(
        activity_code="recreation",
        on_date=date(2026, 9, 10),
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("1"),
        items=(),
        quantity=Decimal("2"),
        benefit_code=None,
    )
    snapshot = ParamSnapshot(
        values=_PARAMS,
        tariffs=(
            TariffFact(
                id=None,
                livestock_group=None,
                coefficient=Decimal("0"),
                quantity_unit="person",
                benefit_modifiers=None,
            ),
        ),
        norm=None,
        load_sb=Decimal("0"),
        load_source="none",
    )
    result = calculate(request, snapshot)
    assert result.amount == Decimal("0")
    assert not any(line.get("kind") == "benefit" for line in result.breakdown)
    return result.amount, result.breakdown


def _exempt_calculation_fields() -> tuple[Decimal, list[dict]]:
    """Ruling #202's zero: `science`, which VMQ 278 prices nowhere. The
    calculator writes `{"kind": "tariff", "reason": "no_tariff_by_law"}`
    ONLY under the explicit, versioned `tariff_exempt:science` parameter
    (migration `0013`; `test_calculator.py::test_science_has_no_tariff_and_
    costs_nothing`) — a merely missing tariff row raises `ERR-NORM-004`
    instead — so the line is a statement of law, never an accident."""
    request = CalcRequest(
        activity_code="science",
        on_date=date(2026, 9, 10),
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("1"),
        items=(),
        quantity=Decimal("1"),
        benefit_code=None,
    )
    snapshot = ParamSnapshot(
        values=_PARAMS | {"tariff_exempt:science": "true"},
        tariffs=(),
        norm=None,
        load_sb=Decimal("0"),
        load_source="none",
    )
    result = calculate(request, snapshot)
    assert result.amount == Decimal("0")
    assert result.breakdown[0]["reason"] == "no_tariff_by_law"
    return result.amount, result.breakdown


async def _approved_application_with_calculation(
    db: AsyncSession,
    *,
    applicant: Applicant,
    grazing_activity_id: uuid.UUID,
    amount: Decimal,
    breakdown: list[dict],
    assigned_user_id: uuid.UUID,
    claim_code: str | None = None,
    claim_status: str = "verified",
    application_activity_id: uuid.UUID | None = None,
) -> Application:
    """Built directly through the ORM, the same idiom `payments/conftest.py`
    `_new_approved_application` uses (`applications` ships no HTTP surface
    for this package's own tests) — with `assigned_user_id` set, so
    `permits.subscribers.on_payment_confirmed` has someone to notify and
    its effect is actually observable rather than a silent early return.
    `activity_type_id` points at the seeded `grazing` row purely to satisfy
    the FK, exactly as every other fixture in this package does — the
    calculator run above is what determines the ACTUAL activity priced."""
    application = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
        status="APPROVED",
        assigned_user_id=assigned_user_id,
        # Ruling #202 pairs the exemption line with the APPLICATION's own
        # activity, the way #185 pairs the benefit line with its claim —
        # `None` (the default every #185 test keeps) can therefore never
        # settle an exemption, which is the fail-closed reading.
        activity_type_id=application_activity_id,
    )
    if claim_code is not None:
        # Stage 10 review, finding 1: the free settlement is tied to the
        # APPLICATION's own verified claim, not to a breakdown line alone —
        # so the positive path needs the claim `0053` seeded, verified.
        application.benefit_category_item_id = await _benefit_item_id(db, claim_code)
        application.benefit_certificate_no = "TEST-0001"
        application.benefit_verification_status = claim_status
    db.add(application)
    await db.flush()
    db.add(
        Calculation(
            application_id=application.id,
            activity_type_id=grazing_activity_id,
            rule_code_version="norms-1.0.0",
            input_snapshot={},
            amount=amount,
            breakdown=breakdown,
        )
    )
    await db.flush()
    return application


async def _benefit_item_id(db: AsyncSession, code: str) -> uuid.UUID:
    """The `benefit_categories` item migration `0053` seeded for `code`."""
    from sqlalchemy import text

    row = (
        await db.execute(
            text(
                "SELECT ci.id FROM classifier_items ci "
                "JOIN classifiers c ON c.id = ci.classifier_id "
                "WHERE c.code = 'benefit_categories' AND ci.code = :code"
            ).bindparams(code=code)
        )
    ).scalar_one()
    return row


async def _executor(db: AsyncSession) -> User:
    return await make_user(db, role_code="executor_staff")


async def test_a_zero_sum_benefit_invoice_settles_itself_end_to_end(
    db: AsyncSession, applicant: Applicant, grazing_activity_id: uuid.UUID
):
    executor = await _executor(db)
    amount, breakdown = _free_calculation_fields("beekeeping_union_member")
    application = await _approved_application_with_calculation(
        db,
        applicant=applicant,
        grazing_activity_id=grazing_activity_id,
        amount=amount,
        breakdown=breakdown,
        assigned_user_id=executor.id,
        claim_code="beekeeping_union_member",
    )

    await publish(db, Event(name=APPLICATION_APPROVED, payload={"application_id": application.id}))
    await db.commit()

    invoice = await payments_service.invoice_for_application(db, application.id)
    assert invoice is not None
    await db.refresh(invoice)
    assert invoice.status == "paid"
    assert invoice.paid_at is not None
    assert invoice.amount == Decimal("0.00")

    # No provider transaction, no ledger allocation — "nothing arrived,
    # nothing to split" (ruling #185).
    provider_txns = await db.scalars(
        select(ProviderTransaction).where(ProviderTransaction.invoice_id == invoice.id)
    )
    assert list(provider_txns) == []
    assert await payments_service.allocations_for(db, invoice.id) == []
    allocations = await db.scalars(select(Allocation).where(Allocation.invoice_id == invoice.id))
    assert list(allocations) == []

    # `is_settled_without_payment`'s own derivation, proven directly.
    assert await payments_service.is_settled_without_payment(db, invoice) is True

    # The application moved INVOICED -> PAID through the SAME function
    # `confirm_payment` uses.
    updated_application = await applications_service.get(db, application.id)
    assert updated_application is not None
    await db.refresh(updated_application)
    assert updated_application.status == "PAID"

    # The audit row ruling #185 names.
    audit_row = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == payments_service.INVOICE_SETTLE_BY_BENEFIT,
                AuditLog.object_id == invoice.id,
            )
        )
    ).scalar_one_or_none()
    assert audit_row is not None
    assert audit_row.new_value == {"benefit_code": "beekeeping_union_member"}

    # The applicant is told there is nothing to pay — event code #185 names,
    # never the ordinary "please pay" one.
    settlement_notification = (
        await db.execute(
            select(Notification).where(
                Notification.event_code == payment_events.INVOICE_SETTLED_BY_BENEFIT,
                Notification.object_id == invoice.id,
                Notification.channel == "inapp",
            )
        )
    ).scalar_one_or_none()
    assert settlement_notification is not None
    assert settlement_notification.recipient_user_id == applicant.owner_user_id
    # The category's NAME, never its code (stage 10 review, finding 8).
    assert "beekeeping_union_member" not in settlement_notification.rendered_text
    assert "Asalarichilar uyushmasi" in settlement_notification.rendered_text
    issued_notification = (
        await db.execute(
            select(Notification).where(
                Notification.event_code == payment_events.INVOICE_ISSUED,
                Notification.object_id == invoice.id,
                Notification.channel == "inapp",
            )
        )
    ).scalar_one_or_none()
    assert issued_notification is None

    # `permits.subscribers.on_payment_confirmed`'s own EFFECT, observed
    # through its stored row — never a mock, never patched. It reads the
    # `payment_confirmed` event `_settle_free` published with the same
    # two-key payload `confirm_payment` uses, and cannot tell the two apart.
    permit_due_notification = (
        await db.execute(
            select(Notification).where(
                Notification.event_code == permit_events.PERMIT_DUE,
                Notification.recipient_user_id == executor.id,
                Notification.object_id == application.id,
                Notification.channel == "inapp",
            )
        )
    ).scalar_one_or_none()
    assert permit_due_notification is not None


async def _issue_and_read(db: AsyncSession, application: Application):
    await publish(db, Event(name=APPLICATION_APPROVED, payload={"application_id": application.id}))
    await db.commit()
    invoice = await payments_service.invoice_for_application(db, application.id)
    assert invoice is not None
    await db.refresh(invoice)
    updated = await applications_service.get(db, application.id)
    assert updated is not None
    await db.refresh(updated)
    return invoice, updated


async def test_a_benefit_line_the_application_never_claimed_does_not_settle(
    db: AsyncSession, applicant: Applicant, grazing_activity_id: uuid.UUID
):
    """Stage 10 review, finding 1 — CONFIRMED before this test existed: a
    head may POST a calculation with any `benefit_code` (`norms` binds it to
    the contour only), so a zero-sum breakdown alone proved nothing about
    the application. No claim at all → the invoice stays `pending` at 0,
    exactly as loud as before ruling #185."""
    executor = await _executor(db)
    amount, breakdown = _free_calculation_fields("war_veterans")
    application = await _approved_application_with_calculation(
        db,
        applicant=applicant,
        grazing_activity_id=grazing_activity_id,
        amount=amount,
        breakdown=breakdown,
        assigned_user_id=executor.id,
        claim_code=None,
    )
    invoice, updated = await _issue_and_read(db, application)
    assert invoice.status == "pending"
    assert updated.status == "INVOICED"
    assert await payments_service.is_settled_without_payment(db, invoice) is False


async def test_an_unverified_claim_does_not_settle(
    db: AsyncSession, applicant: Applicant, grazing_activity_id: uuid.UUID
):
    """The claim exists but nobody verified it (`pending`) — the leshoz's
    check (#182) is what makes the zero lawful, so the settlement waits."""
    executor = await _executor(db)
    amount, breakdown = _free_calculation_fields("war_veterans")
    application = await _approved_application_with_calculation(
        db,
        applicant=applicant,
        grazing_activity_id=grazing_activity_id,
        amount=amount,
        breakdown=breakdown,
        assigned_user_id=executor.id,
        claim_code="war_veterans",
        claim_status="pending",
    )
    invoice, updated = await _issue_and_read(db, application)
    assert invoice.status == "pending"
    assert updated.status == "INVOICED"


async def test_a_verified_claim_of_another_category_does_not_settle(
    db: AsyncSession, applicant: Applicant, grazing_activity_id: uuid.UUID
):
    """Verified as `preschool_children`, priced as `war_veterans`: the line
    and the claim disagree, and a disagreement is not a free permit."""
    executor = await _executor(db)
    amount, breakdown = _free_calculation_fields("war_veterans")
    application = await _approved_application_with_calculation(
        db,
        applicant=applicant,
        grazing_activity_id=grazing_activity_id,
        amount=amount,
        breakdown=breakdown,
        assigned_user_id=executor.id,
        claim_code="preschool_children",
    )
    invoice, updated = await _issue_and_read(db, application)
    assert invoice.status == "pending"
    assert updated.status == "INVOICED"


async def test_a_zero_with_no_benefit_line_stays_a_plain_pending_invoice(
    db: AsyncSession, applicant: Applicant, grazing_activity_id: uuid.UUID
):
    """The fail-closed half of the ruling: a tariff row published at a
    genuine zero coefficient must never be read as a benefit."""
    executor = await _executor(db)
    amount, breakdown = _plain_zero_calculation_fields()
    application = await _approved_application_with_calculation(
        db,
        applicant=applicant,
        grazing_activity_id=grazing_activity_id,
        amount=amount,
        breakdown=breakdown,
        assigned_user_id=executor.id,
    )

    await publish(db, Event(name=APPLICATION_APPROVED, payload={"application_id": application.id}))
    await db.commit()

    invoice = await payments_service.invoice_for_application(db, application.id)
    assert invoice is not None
    await db.refresh(invoice)
    assert invoice.status == "pending"
    assert invoice.paid_at is None
    assert await payments_service.is_settled_without_payment(db, invoice) is False

    updated_application = await applications_service.get(db, application.id)
    assert updated_application is not None
    await db.refresh(updated_application)
    assert updated_application.status == "INVOICED"

    # The ordinary "please pay" notification, not the settlement one.
    issued_notification = (
        await db.execute(
            select(Notification).where(
                Notification.event_code == payment_events.INVOICE_ISSUED,
                Notification.object_id == invoice.id,
                Notification.channel == "inapp",
            )
        )
    ).scalar_one_or_none()
    assert issued_notification is not None
    settlement_notification = (
        await db.execute(
            select(Notification).where(
                Notification.event_code == payment_events.INVOICE_SETTLED_BY_BENEFIT,
                Notification.object_id == invoice.id,
                Notification.channel == "inapp",
            )
        )
    ).scalar_one_or_none()
    assert settlement_notification is None


async def test_settled_without_payment_appears_in_the_api_output_for_both_cases(
    db: AsyncSession,
    applicant: Applicant,
    grazing_activity_id: uuid.UUID,
    payments_view_client,
):
    """Plan B4 item 3: the STAFF reader's own `GET /invoices/{id}` (the
    accountant, `payments.view` — `_invoice_out`'s own gate) must say which
    invoice needs no payment and which one still does."""
    executor = await _executor(db)

    free_amount, free_breakdown = _free_calculation_fields("war_veterans")
    free_application = await _approved_application_with_calculation(
        db,
        applicant=applicant,
        grazing_activity_id=grazing_activity_id,
        amount=free_amount,
        breakdown=free_breakdown,
        assigned_user_id=executor.id,
        claim_code="war_veterans",
    )
    await publish(
        db, Event(name=APPLICATION_APPROVED, payload={"application_id": free_application.id})
    )
    await db.commit()
    free_invoice = await payments_service.invoice_for_application(db, free_application.id)
    assert free_invoice is not None

    response = await payments_view_client.get(f"{API}/invoices/{free_invoice.id}")
    assert response.status_code == 200, response.text
    assert response.json()["settled_without_payment"] is True

    # A SECOND application for the SAME applicant is legal here: the
    # `EXCLUDE` guard the `Application` model docstring names only fires
    # once `contour_id`/`period_from`/`period_to` are all set, and neither
    # fixture here sets any of them.
    plain_amount, plain_breakdown = _plain_zero_calculation_fields()
    plain_application = await _approved_application_with_calculation(
        db,
        applicant=applicant,
        grazing_activity_id=grazing_activity_id,
        amount=plain_amount,
        breakdown=plain_breakdown,
        assigned_user_id=executor.id,
    )
    await publish(
        db, Event(name=APPLICATION_APPROVED, payload={"application_id": plain_application.id})
    )
    await db.commit()
    plain_invoice = await payments_service.invoice_for_application(db, plain_application.id)
    assert plain_invoice is not None

    response = await payments_view_client.get(f"{API}/invoices/{plain_invoice.id}")
    assert response.status_code == 200, response.text
    assert response.json()["settled_without_payment"] is False


async def test_the_manual_confirmation_door_still_refuses_amount_zero(
    db: AsyncSession,
    applicant: Applicant,
    grazing_activity_id: uuid.UUID,
    payments_view_client,
):
    """Plan B4 item 4: `_settle_free` never runs for a plain zero (no
    benefit line), so the invoice stays `pending` — and the ONE other door
    that could turn a zero invoice `paid` (`backoffice`'s maker-checker
    manual confirmation, `ManualConfirmationIn.amount` bounded `gt=0`)
    refuses it too, before any of this module's own business logic runs."""
    executor = await _executor(db)
    amount, breakdown = _plain_zero_calculation_fields()
    application = await _approved_application_with_calculation(
        db,
        applicant=applicant,
        grazing_activity_id=grazing_activity_id,
        amount=amount,
        breakdown=breakdown,
        assigned_user_id=executor.id,
    )
    await publish(db, Event(name=APPLICATION_APPROVED, payload={"application_id": application.id}))
    await db.commit()
    invoice = await payments_service.invoice_for_application(db, application.id)
    assert invoice is not None
    assert invoice.status == "pending"

    bank_doc = MediaFile(
        storage_key=f"bank-docs/{uuid.uuid4().hex}.pdf",
        filename="payment-order.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    )
    db.add(bank_doc)
    await db.commit()

    response = await payments_view_client.post(
        f"{API}/payments/manual-confirmations",
        json={
            "invoice_id": str(invoice.id),
            "amount": "0",
            "paid_at": "2026-09-10T10:00:00Z",
            "bank_doc_file_id": str(bank_doc.id),
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ERR-VAL-001"

    await db.refresh(invoice)
    assert invoice.status == "pending"


# --- Ruling #202: a statutory exemption is the other lawful zero -----------


@pytest.fixture
async def platform_fixed_15000(engine) -> AsyncIterator[PaymentRecipient]:
    """The dev stand's own directory on 2026-09-10 — a FIXED 15 000 receiver
    («platforma uchun») — which turned every `science` approval into
    `ERR-VAL-001`/`split_does_not_fit` ("configured shares total 15000.00
    on a payment of 0.00") and would have done the same to every verified
    100 % benefit.

    NOT built on `db` the way `test_invoice_snapshot.py::fund_fixed_50000`
    is: that test's `issue_invoice` RAISES, so nothing there ever commits,
    while every test here settles the invoice and `_issue_and_read` commits
    `db` — a row flushed on it would persist into the shared, never-empty
    test database and stack a further 15 000 onto every later test's split
    (30 000, 45 000 … was the first run's own symptom). Written and deleted
    through the `engine`'s own session instead, the `budget_50_inactive`
    idiom; the snapshot rows that point at it go first (FK)."""
    factory = make_session_factory(engine)
    row = PaymentRecipient(
        name={"uz_latn": "platforma uchun"},
        kind="fixed",
        fixed_amount=Decimal("15000.00"),
    )
    async with factory() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    yield row
    async with factory() as session:
        await session.execute(
            delete(InvoiceRecipient).where(InvoiceRecipient.recipient_id == row.id)
        )
        await session.execute(delete(PaymentRecipient).where(PaymentRecipient.id == row.id))
        await session.commit()


async def test_a_statutory_exemption_settles_itself_end_to_end(
    db: AsyncSession,
    applicant: Applicant,
    grazing_activity_id: uuid.UUID,
    science_activity_id: uuid.UUID,
):
    executor = await _executor(db)
    amount, breakdown = _exempt_calculation_fields()
    application = await _approved_application_with_calculation(
        db,
        applicant=applicant,
        grazing_activity_id=grazing_activity_id,
        amount=amount,
        breakdown=breakdown,
        assigned_user_id=executor.id,
        application_activity_id=science_activity_id,
    )

    invoice, updated = await _issue_and_read(db, application)
    assert invoice.status == "paid"
    assert invoice.paid_at is not None
    assert invoice.amount == Decimal("0.00")
    assert updated.status == "PAID"

    # Nothing arrived, nothing to split — no provider transaction, no
    # ledger allocation, exactly as for a benefit (#185).
    provider_txns = await db.scalars(
        select(ProviderTransaction).where(ProviderTransaction.invoice_id == invoice.id)
    )
    assert list(provider_txns) == []
    assert await payments_service.allocations_for(db, invoice.id) == []
    assert await payments_service.is_settled_without_payment(db, invoice) is True

    # The audit row names the LAW's reason, never a benefit.
    audit_row = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == payments_service.INVOICE_SETTLE_BY_LAW,
                AuditLog.object_id == invoice.id,
            )
        )
    ).scalar_one_or_none()
    assert audit_row is not None
    assert audit_row.new_value == {"activity_code": "science"}
    benefit_audit_row = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == payments_service.INVOICE_SETTLE_BY_BENEFIT,
                AuditLog.object_id == invoice.id,
            )
        )
    ).scalar_one_or_none()
    assert benefit_audit_row is None

    # The applicant is told there is nothing to pay, in the exemption's own
    # words — the activity's NAME, never its code, and never the benefit
    # template's «imtiyozi qo'llanildi».
    settlement_notification = (
        await db.execute(
            select(Notification).where(
                Notification.event_code == payment_events.INVOICE_SETTLED_BY_LAW,
                Notification.object_id == invoice.id,
                Notification.channel == "inapp",
            )
        )
    ).scalar_one_or_none()
    assert settlement_notification is not None
    assert settlement_notification.recipient_user_id == applicant.owner_user_id
    assert "science" not in settlement_notification.rendered_text
    assert "Ilmiy tadqiqot" in settlement_notification.rendered_text
    assert "imtiyoz" not in settlement_notification.rendered_text
    for other_code in (payment_events.INVOICE_ISSUED, payment_events.INVOICE_SETTLED_BY_BENEFIT):
        other = (
            await db.execute(
                select(Notification).where(
                    Notification.event_code == other_code,
                    Notification.object_id == invoice.id,
                    Notification.channel == "inapp",
                )
            )
        ).scalar_one_or_none()
        assert other is None, other_code

    # `permits.subscribers.on_payment_confirmed` ran, through the same
    # `payment_confirmed` event a real payment publishes.
    permit_due_notification = (
        await db.execute(
            select(Notification).where(
                Notification.event_code == permit_events.PERMIT_DUE,
                Notification.recipient_user_id == executor.id,
                Notification.object_id == application.id,
                Notification.channel == "inapp",
            )
        )
    ).scalar_one_or_none()
    assert permit_due_notification is not None


async def test_a_statutory_exemption_settles_under_a_fixed_amount_receiver(
    db: AsyncSession,
    applicant: Applicant,
    grazing_activity_id: uuid.UUID,
    science_activity_id: uuid.UUID,
    platform_fixed_15000: PaymentRecipient,
):
    """The defect as reported (dev stand, 2026-09-10): with a fixed 15 000
    receiver configured, `POST /applications/{id}/approve` on a `science`
    application answered 422 `split_does_not_fit` — the head could not
    approve at all. The snapshot (#158) is still frozen, every share at 0."""
    executor = await _executor(db)
    amount, breakdown = _exempt_calculation_fields()
    application = await _approved_application_with_calculation(
        db,
        applicant=applicant,
        grazing_activity_id=grazing_activity_id,
        amount=amount,
        breakdown=breakdown,
        assigned_user_id=executor.id,
        application_activity_id=science_activity_id,
    )

    invoice, updated = await _issue_and_read(db, application)
    assert invoice.status == "paid"
    assert updated.status == "PAID"

    rows = await payments_service.invoice_recipients(db, invoice.id)
    by_recipient = {row.recipient_id: row for row in rows}
    assert by_recipient[platform_fixed_15000.id].kind == "fixed"
    assert by_recipient[platform_fixed_15000.id].fixed_amount == Decimal("15000.00")
    assert by_recipient[platform_fixed_15000.id].amount == Decimal("0.00")
    assert rows[-1].kind == payments_service.SNAPSHOT_KIND_REMAINDER
    assert rows[-1].amount == Decimal("0.00")
    assert sum(row.amount for row in rows) == Decimal("0.00")


async def test_a_benefit_zero_settles_under_a_fixed_amount_receiver(
    db: AsyncSession,
    applicant: Applicant,
    grazing_activity_id: uuid.UUID,
    platform_fixed_15000: PaymentRecipient,
):
    """The same fixed receiver blocked every verified 100 % benefit too —
    the split check ran BEFORE ruling #185's own branch."""
    executor = await _executor(db)
    amount, breakdown = _free_calculation_fields("war_veterans")
    application = await _approved_application_with_calculation(
        db,
        applicant=applicant,
        grazing_activity_id=grazing_activity_id,
        amount=amount,
        breakdown=breakdown,
        assigned_user_id=executor.id,
        claim_code="war_veterans",
    )
    invoice, updated = await _issue_and_read(db, application)
    assert invoice.status == "paid"
    assert updated.status == "PAID"
    rows = await payments_service.invoice_recipients(db, invoice.id)
    assert {row.amount for row in rows} == {Decimal("0.00")}


async def test_an_exemption_line_for_another_activity_does_not_settle(
    db: AsyncSession,
    applicant: Applicant,
    grazing_activity_id: uuid.UUID,
):
    """The fail-closed half, mirroring #185's stage-10 finding 1: a head may
    bind a calculation the calculator priced as `science` to a GRAZING
    application (`save_calculation` guards the contour, not the activity —
    ruling 20), and a zero-sum `no_tariff_by_law` line alone proves nothing
    about THIS application. Priced as science, filed as grazing → the
    invoice stays `pending` at 0, exactly as loud as before."""
    executor = await _executor(db)
    amount, breakdown = _exempt_calculation_fields()
    application = await _approved_application_with_calculation(
        db,
        applicant=applicant,
        grazing_activity_id=grazing_activity_id,
        amount=amount,
        breakdown=breakdown,
        assigned_user_id=executor.id,
        application_activity_id=grazing_activity_id,
    )
    invoice, updated = await _issue_and_read(db, application)
    assert invoice.status == "pending"
    assert updated.status == "INVOICED"
    assert await payments_service.is_settled_without_payment(db, invoice) is False


async def test_an_exemption_line_on_an_application_with_no_activity_does_not_settle(
    db: AsyncSession,
    applicant: Applicant,
    grazing_activity_id: uuid.UUID,
):
    """`applications.activity_type_id` is nullable; nothing to pair the line
    with means no settlement, never a free permit by default."""
    executor = await _executor(db)
    amount, breakdown = _exempt_calculation_fields()
    application = await _approved_application_with_calculation(
        db,
        applicant=applicant,
        grazing_activity_id=grazing_activity_id,
        amount=amount,
        breakdown=breakdown,
        assigned_user_id=executor.id,
    )
    invoice, updated = await _issue_and_read(db, application)
    assert invoice.status == "pending"
    assert updated.status == "INVOICED"


async def test_settled_without_payment_is_true_for_an_exemption_in_the_api_output(
    db: AsyncSession,
    applicant: Applicant,
    grazing_activity_id: uuid.UUID,
    science_activity_id: uuid.UUID,
    payments_view_client,
):
    executor = await _executor(db)
    amount, breakdown = _exempt_calculation_fields()
    application = await _approved_application_with_calculation(
        db,
        applicant=applicant,
        grazing_activity_id=grazing_activity_id,
        amount=amount,
        breakdown=breakdown,
        assigned_user_id=executor.id,
        application_activity_id=science_activity_id,
    )
    await publish(db, Event(name=APPLICATION_APPROVED, payload={"application_id": application.id}))
    await db.commit()
    invoice = await payments_service.invoice_for_application(db, application.id)
    assert invoice is not None

    response = await payments_view_client.get(f"{API}/invoices/{invoice.id}")
    assert response.status_code == 200, response.text
    assert response.json()["settled_without_payment"] is True
