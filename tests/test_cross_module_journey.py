"""The whole citizen journey across the three modules built in parallel.

`applications` (3.9a), `payments` (3.10a) and `permits` (3.11a) were written by
separate sessions that could not see each other's code, and each was reviewed on
its own branch. Every test they left behind stops at its own module's edge:
`permits`' `paid_application` fixture builds `status="PAID"` through the ORM
instead of paying, and `payments`' end-to-end test stops at PAID saying "3.11 has
everything it needs". Nothing ran the two halves together, so nothing could see
the seam.

**Top level, not inside a module package, because it belongs to none of them.**
The fixtures are imported from `tests/modules/permits/conftest.py` as plain
importables — the idiom `permits`, `norms` and `payments` already use for gis's
spatial primitives; two ways to build a paid application is how the two drift
apart. `_app_on_test_db` comes with them, since an autouse fixture applies only
inside its own package and this module is outside all three.

The path driven here is the real one, with real service functions and real bus
events — no stubs, no hand-set statuses:

    APPROVED --(bus: application_approved)--> INVOICED
      --> confirm_payment --> PAID --(bus: payment_confirmed)--> executor told
      --> issue --> 4 ERI signatures --> ACTIVE --> application PERMIT_ISSUED
      --> the anonymous public QR check finds it

and it asserts the three things only a cross-module test can see:

  1. **the bus actually carried each hop** — by its effect, not by a spy: with
     `payments`' subscriber unwired the application never leaves APPROVED and no
     invoice exists; with `permits`' unwired the assigned executor is never told
     a permit is due. Both are silent in production and green in every
     single-module test;
  2. **the amount the citizen was billed is the amount printed on the permit** —
     two modules that may not read each other, each reading the price from
     `applications.service.current_calculation` on its own;
  3. **the same `calculation_id` is on the invoice and in the permit's frozen
     snapshot** — the only link that lets a paid invoice be reconciled with the
     document it paid for.

Shared-test-DB discipline (lesson): every PINFL and every certificate serial
here comes from the imported fixtures' own randomised generators, never a
literal — these fixtures COMMIT their users, `users.pinfl` is unique and
`certificates` is unique on `(serial_number, issuer)`, so a literal passes once
and fails on the second run of the suite.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, publish
from app.modules.applications import service as applications_service
from app.modules.applications.events import APPLICATION_APPROVED
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, User
from app.modules.norms.calculator import RULE_CODE_VERSION
from app.modules.norms.models import Calculation
from app.modules.notifications.models import Notification
from app.modules.payments import service as payments_service
from app.modules.payments.models import ProviderTransaction
from app.modules.permits import events as permit_events
from app.modules.permits import service as permits_service
from app.modules.permits import signers
from tests.modules.auth.test_sessions import make_user
from tests.modules.permits.conftest import Signer, _signer_for, sign_permit

# The permits package's own fixtures, re-exported so pytest can inject them here.
# `_app_on_test_db` is autouse and must travel with them: without it `create_app()`
# opens the shared DEV database and every request 401s with no hint that the
# database is the bug (lesson).
from tests.modules.permits.conftest import _app_on_test_db as _app_on_test_db  # noqa: F401
from tests.modules.permits.conftest import accountant_client as accountant_client  # noqa: F401
from tests.modules.permits.conftest import approval_doc as approval_doc  # noqa: F401
from tests.modules.permits.conftest import (
    approved_application as approved_application,  # noqa: F401
)
from tests.modules.permits.conftest import (
    chief_forester_client as chief_forester_client,  # noqa: F401
)
from tests.modules.permits.conftest import client as client  # noqa: F401
from tests.modules.permits.conftest import contours_layer as contours_layer  # noqa: F401
from tests.modules.permits.conftest import grazing_activity_id as grazing_activity_id
from tests.modules.permits.conftest import head_client as head_client  # noqa: F401
from tests.modules.permits.conftest import hodim_client as hodim_client  # noqa: F401
from tests.modules.permits.conftest import leshoz as leshoz

API = "/api/v1"


async def _reread(db: AsyncSession, row):
    """ONE refreshing read for every assertion on a row the APP may have changed.

    The `db` fixture's session and the app's session never see each other's
    current state, and `db.refresh` remembered at one call site and forgotten at
    the next is what failed twice already (lesson) — so nothing in this file
    asserts on `application.status` or `permit.status` without going through
    here.
    """
    await db.refresh(row)
    return row


@pytest.fixture
async def journey_holder(db: AsyncSession, approved_application: Application):
    """The recipient signatory: the applicant who OWNS this journey's own
    application.

    `permits`' `holder_client` cannot stand in — it belongs to the separate
    `paid_application` fixture, and the recipient signature is refused for
    anyone but the permit's own holder.
    """
    applicant = await db.get(Applicant, approved_application.applicant_id)
    assert applicant is not None and applicant.owner_user_id is not None
    user = await db.get(User, applicant.owner_user_id)
    assert user is not None
    async for signer in _signer_for(db, role_code="applicant", user=user):
        yield signer


@pytest.fixture
async def journey_executor(db: AsyncSession, approved_application: Application, leshoz) -> User:
    """The hodim the application is assigned to — the recipient of the
    `permit.due` notification that proves the `payment_confirmed` hop happened.

    Without an assignee `permits.subscribers.on_payment_confirmed` returns early
    and notifies nobody, which is correct behaviour and would make hop 2
    unobservable here.
    """
    user = await make_user(db, role_code="executor_staff", organization_id=leshoz.id)
    approved_application.assigned_user_id = user.id
    await db.flush()
    return user


async def test_the_journey_from_approval_to_the_public_qr_page(
    db: AsyncSession,
    client: httpx.AsyncClient,
    hodim_client: httpx.AsyncClient,
    head_client: Signer,
    chief_forester_client: Signer,
    accountant_client: Signer,
    journey_holder: Signer,
    journey_executor: User,
    approved_application: Application,
):
    """One approved application, all the way to a permit an inspector can check.

    Every hop is driven by the code that drives it in production: the bus event
    `applications` publishes on approval, `payments`' own `confirm_payment`, the
    `POST /applications/{id}/permit` route, the four ERI signature posts, and the
    anonymous `GET /public/permits/check`.
    """
    app_id = approved_application.id

    # --- hop 1: APPROVED -> INVOICED, carried by the bus ----------------------
    # `payments.subscribers.on_application_approved` runs INSIDE this publish,
    # in this very transaction. Unwire it and the two assertions below both go
    # red — which is the only place in the suite where that is true.
    await publish(db, Event(name=APPLICATION_APPROVED, payload={"application_id": app_id}))
    await db.commit()

    application = await applications_service.get(db, app_id)
    assert application is not None
    assert (await _reread(db, application)).status == "INVOICED"

    invoice = await payments_service.invoice_for_application(db, app_id)
    assert invoice is not None, "the bus hop application_approved -> issue_invoice did not happen"
    await db.refresh(invoice)
    billed_amount = invoice.amount
    billed_calculation_id = invoice.calculation_id

    # --- hop 2: the money arrives -------------------------------------------
    # `confirm_payment` is the real one: it moves the invoice and the
    # application, writes the ledger, notifies the payer and publishes
    # `payment_confirmed`.
    transaction = ProviderTransaction(
        invoice_id=invoice.id,
        provider="payme",
        external_id=f"journey-{uuid.uuid4().hex[:12]}",
        amount=invoice.amount,
        state="2",
        performed_at=datetime.now(UTC),
        payload={},
    )
    db.add(transaction)
    await db.flush()
    await payments_service.confirm_payment(db, invoice=invoice, transaction=transaction)
    await db.commit()

    assert (await _reread(db, application)).status == "PAID"
    assert await payments_service.is_paid(db, app_id) is True

    # --- hop 3: the executor learns a permit is due --------------------------
    # `permits.subscribers.on_payment_confirmed` heard `payment_confirmed` and
    # notified the assignee — and NOTHING else: issuance is a human act (ruling
    # 19), so no permit exists yet.
    due = list(
        await db.scalars(
            select(Notification).where(
                Notification.event_code == permit_events.PERMIT_DUE,
                Notification.recipient_user_id == journey_executor.id,
                Notification.object_id == app_id,
            )
        )
    )
    assert due, "the bus hop payment_confirmed -> permits.on_payment_confirmed did not happen"
    # The amount on that notification is read back from the calculation, never
    # carried on the event — so it is the third independent reader of the price.
    assert {row.params["amount"] for row in due} == {str(billed_amount)}
    assert await permits_service.for_application(db, app_id) is None

    # --- hop 4: a human forms the document -----------------------------------
    issued = await hodim_client.post(f"{API}/applications/{app_id}/permit")
    assert issued.status_code == 201, issued.text
    permit_id = uuid.UUID(issued.json()["id"])

    # The holder can fetch their own document; those are the bytes every
    # signature is taken over.
    pdf_response = await journey_holder.client.get(f"{API}/permits/{permit_id}/pdf")
    assert pdf_response.status_code == 200, pdf_response.text
    pdf = pdf_response.content

    # Issuance does NOT move the application (ruling 18) — «сформировано **и
    # подписано**» is what PERMIT_ISSUED means.
    assert (await _reread(db, application)).status == "PAID"

    # --- hop 5: the 3+1 ERI signatures ---------------------------------------
    for signer, purpose in (
        (head_client, "permit_head"),
        (chief_forester_client, "permit_chief_forester"),
        (accountant_client, "permit_accountant"),
        (journey_holder, signers.RECIPIENT_PURPOSE),
    ):
        result = await sign_permit(signer, permit_id, purpose, pdf)
        assert result.status_code == 200, (purpose, result.text)

    permit = await permits_service.get(db, permit_id)
    assert permit is not None
    assert (await _reread(db, permit)).status == "active"
    assert (await _reread(db, application)).status == "PERMIT_ISSUED"

    # --- hop 6: the anonymous public check ------------------------------------
    public = await client.get(f"{API}/public/permits/check", params={"qr": permit.qr_token})
    assert public.status_code == 200, public.text
    body = public.json()
    assert body["found"] is True
    # The card speaks the citizen's language, so the label comes from the
    # module's own map rather than a literal (`PUBLIC_STATUS_LABELS`).
    assert body["status"] == permits_service.PUBLIC_STATUS_LABELS["active"]

    # --- what only this test can see: the money is ONE number ----------------
    # `payments.issue_invoice` and `permits.issue` each call
    # `applications.service.current_calculation` independently, and being both
    # level 4 they cannot compare notes. Here they can be compared.
    assert permit.amount == billed_amount, (
        f"the permit prints {permit.amount} but the citizen was billed {billed_amount}"
    )
    assert billed_calculation_id is not None
    assert str(permit.snapshot["calculation_id"]) == str(billed_calculation_id), (
        "the invoice and the permit's frozen snapshot name different calculations, "
        "so a paid invoice cannot be reconciled with the document it paid for"
    )


async def test_the_permit_is_priced_from_the_calculation_the_invoice_froze(
    db: AsyncSession,
    hodim_client: httpx.AsyncClient,
    approved_application: Application,
    grazing_activity_id: uuid.UUID,
):
    """A NEWER calculation lands between invoicing and issuance: the permit must
    still print what the citizen was billed.

    This is the audit's own probe, kept: it inserted a 9 999 999,00 calculation
    after a 2 060 000,00 invoice had been paid and the permit printed the new
    number, with a different `calculation_id` on the invoice and in the
    snapshot. It is unreachable through any HTTP route today — see
    `test_a_calculation_cannot_be_attached_to_an_application_through_the_write_path`
    below for the accident that closes it, and `permits.service.issue` for the
    half `permits` can refuse on its own.
    """
    from tests.modules.permits.conftest import GRAZING_HERD, calculation_input_snapshot

    app_id = approved_application.id
    await publish(db, Event(name=APPLICATION_APPROVED, payload={"application_id": app_id}))
    await db.commit()

    invoice = await payments_service.invoice_for_application(db, app_id)
    assert invoice is not None
    await db.refresh(invoice)
    billed_amount = invoice.amount
    billed_calculation_id = invoice.calculation_id

    # The recalculation. `decided_at` is what makes it refusable from inside
    # `permits`: a price computed after the decision is not the price that was
    # billed. 3.9b's flow sets it on approval; 3.9a branch 1 has no decision
    # verb, so the two timestamps are set explicitly here — `created_at`
    # included, because Postgres' `now()` is the TRANSACTION's clock and every
    # row written by this test would otherwise share one instant.
    approved_at = datetime.now(UTC)
    application = await applications_service.get(db, app_id)
    assert application is not None
    application.decided_at = approved_at
    await db.flush()
    db.add(
        Calculation(
            application_id=app_id,
            contour_id=approved_application.contour_id,
            activity_type_id=grazing_activity_id,
            rule_code_version=RULE_CODE_VERSION,
            input_snapshot=calculation_input_snapshot(GRAZING_HERD),
            used_sb=Decimal("40.0000"),
            amount=Decimal("9999999.00"),
            breakdown={"total": "9999999.00"},
            created_at=approved_at + timedelta(minutes=5),
        )
    )
    await db.flush()

    transaction = ProviderTransaction(
        invoice_id=invoice.id,
        provider="payme",
        external_id=f"journey-{uuid.uuid4().hex[:12]}",
        amount=invoice.amount,
        state="2",
        performed_at=datetime.now(UTC),
        payload={},
    )
    db.add(transaction)
    await db.flush()
    await payments_service.confirm_payment(db, invoice=invoice, transaction=transaction)
    await db.commit()

    issued = await hodim_client.post(f"{API}/applications/{app_id}/permit")
    assert issued.status_code == 422, issued.text
    assert issued.json()["error"]["details"]["reason"] == "calculation_after_decision"

    # And nothing was formed: a permit printing 9 999 999,00 against a paid
    # 2 060 000,00 invoice must not exist at all.
    permit = await permits_service.for_application(db, app_id)
    assert permit is None, (
        f"a permit was issued for {billed_amount} billed under calculation {billed_calculation_id}"
    )


async def test_a_calculation_cannot_be_attached_to_an_application_through_the_write_path(
    db: AsyncSession,
    hodim_client: httpx.AsyncClient,
    approved_application: Application,
    grazing_activity_id: uuid.UUID,
):
    """**This test stands in for a guard that does not exist yet.**

    `payments.issue_invoice` and `permits.issue` each read "the newest
    calculation for this application" independently. Nothing compares the two,
    and being both level 4 they cannot. If a newer calculation could be attached
    to an application after it was invoiced, the citizen would be billed
    2 060 000,00 and the permit would print 9 999 999,00 — demonstrated by the
    test above, which has to reach past the API to build that state.

    It is unreachable through the public write path TODAY only because
    `norms.schemas.CalculationIn.application_id` is typed `None = None`, so
    `POST /norms/calculations` cannot bind a calculation to an application at
    all — an accident of 3.7's fail-closed edge (I4), not a designed guard.
    `norms.save_calculation` itself has no application-status check of any kind.

    **Stage 3.9 opens that field.** Whoever does must land, in the same commit,
    the guard that refuses a calculation for an application at APPROVED or
    beyond (3.9b's own ruling already says so) — and then rewrite this test to
    assert THAT refusal. Deleting it because the field now accepts a value is
    exactly the failure it exists to catch.
    """
    from app.modules.norms.schemas import CalculationIn

    # The schema field, not the route: `CalculationIn` is what the write route
    # validates against, and pydantic refuses anything but None for it.
    period_from, period_to = approved_application.period_from, approved_application.period_to
    assert period_from is not None and period_to is not None
    with pytest.raises(ValueError):
        CalculationIn(
            application_id=approved_application.id,  # type: ignore[arg-type]
            contour_id=uuid.uuid4(),
            activity_type_id=grazing_activity_id,
            period_from=period_from,
            period_to=period_to,
        )

    assert CalculationIn.model_fields["application_id"].annotation is type(None), (
        "CalculationIn.application_id has been opened up — see this test's docstring: "
        "payments and permits each read the NEWEST calculation and cannot compare notes, "
        "so an application-status guard must land in `applications`/`norms` in the same "
        "commit, and this test must be rewritten to assert it"
    )
