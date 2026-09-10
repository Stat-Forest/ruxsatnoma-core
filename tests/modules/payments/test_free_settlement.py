"""Ruling #185 (stage 10, track B4): a zero-sum invoice from a benefit
settles itself; a zero from anywhere else is still a refusal.

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
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, publish
from app.core.models import MediaFile
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
from app.modules.payments.models import Allocation, ProviderTransaction
from app.modules.permits import events as permit_events
from tests.modules.auth.test_sessions import make_user

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


async def _approved_application_with_calculation(
    db: AsyncSession,
    *,
    applicant: Applicant,
    grazing_activity_id: uuid.UUID,
    amount: Decimal,
    breakdown: list[dict],
    assigned_user_id: uuid.UUID,
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
    )
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

    # `is_settled_by_benefit`'s own derivation, proven directly.
    assert await payments_service.is_settled_by_benefit(db, invoice) is True

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
    assert "beekeeping_union_member" in settlement_notification.rendered_text
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
    assert await payments_service.is_settled_by_benefit(db, invoice) is False

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


async def test_settled_by_benefit_appears_in_the_api_output_for_both_cases(
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
    )
    await publish(
        db, Event(name=APPLICATION_APPROVED, payload={"application_id": free_application.id})
    )
    await db.commit()
    free_invoice = await payments_service.invoice_for_application(db, free_application.id)
    assert free_invoice is not None

    response = await payments_view_client.get(f"{API}/invoices/{free_invoice.id}")
    assert response.status_code == 200, response.text
    assert response.json()["settled_by_benefit"] is True

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
    assert response.json()["settled_by_benefit"] is False


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
