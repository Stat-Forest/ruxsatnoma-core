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

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, publish
from app.core.models import MediaFile
from app.modules.admin.models import Organization
from app.modules.applications import service as applications_service
from app.modules.applications.events import APPLICATION_APPROVED
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, User
from app.modules.gis.models import GisLayer
from app.modules.norms.calculator import RULE_CODE_VERSION
from app.modules.norms.models import Calculation
from app.modules.norms.schemas import CalculationIn
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
from tests.modules.permits.conftest import applicant_client as applicant_client  # noqa: F401
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


async def test_the_manual_confirmation_door_reaches_every_downstream_module_too(
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
    """The Payme branch above (plan `03.10b-payments-reconciliation` task
    10) proves the bus/signature/QR chain once through the PROVIDER door;
    this proves the OTHER legal way an invoice becomes `paid` — `tz/08` §4's
    maker-checker manual confirmation — reaches every one of the same
    downstream modules.

    No new actor is introduced: `accountant_client` (`accountant`, holds
    `payments.manage`) FILES and `head_client` (`executor_head`, holds
    `payments.confirm`) DECIDES — the same two roles this file already needs
    a few lines below for two of the four ERI signatures. Only a new ACTION
    by actors already in the journey.

    Hop 2 is the only thing this test does differently from the Payme
    version above: `POST /payments/manual-confirmations` then
    `.../confirm`, never a `ProviderTransaction` built by hand. The checker's
    approval pays through the exact same `payments.service.confirm_payment`
    the Payme webhook calls (ruling 14), so hops 1 and 3-6 are asserted
    IDENTICALLY to the Payme test above — a divergence between the two doors
    downstream of `confirm_payment` is exactly the seam this test exists to
    catch.
    """
    app_id = approved_application.id

    # --- hop 1: APPROVED -> INVOICED, carried by the bus ----------------------
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

    # --- hop 2: the money arrives, through the MANUAL door --------------------
    bank_doc = MediaFile(
        storage_key=f"journey-bank-docs/{uuid.uuid4().hex}.pdf",
        filename="payment-order.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    )
    db.add(bank_doc)
    await db.commit()

    filed = await accountant_client.client.post(
        f"{API}/payments/manual-confirmations",
        json={
            "invoice_id": str(invoice.id),
            "amount": str(invoice.amount),
            "paid_at": datetime.now(UTC).isoformat(),
            "bank_doc_file_id": str(bank_doc.id),
        },
    )
    assert filed.status_code == 201, filed.text
    confirmation_id = filed.json()["id"]

    confirmed = await head_client.client.post(
        f"{API}/payments/manual-confirmations/{confirmation_id}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text

    assert (await _reread(db, application)).status == "PAID"
    # `is_paid` reads the invoice back through its OWN query, but the
    # object it resolves to is already in this session's identity map from
    # the `invoice_for_application` call above — a `SELECT` does not refresh
    # an already-loaded instance's attributes (lesson: "the db fixture
    # session and the app's session never see each other's current state").
    # `_reread` is exactly this fix, applied to `invoice` instead of
    # `application`.
    await _reread(db, invoice)
    assert await payments_service.is_paid(db, app_id) is True

    # --- hop 3: the executor learns a permit is due --------------------------
    due = list(
        await db.scalars(
            select(Notification).where(
                Notification.event_code == permit_events.PERMIT_DUE,
                Notification.recipient_user_id == journey_executor.id,
                Notification.object_id == app_id,
            )
        )
    )
    assert due, "the manual door did not carry payment_confirmed to permits.on_payment_confirmed"
    assert {row.params["amount"] for row in due} == {str(billed_amount)}
    assert await permits_service.for_application(db, app_id) is None

    # --- hop 4: a human forms the document -----------------------------------
    issued = await hodim_client.post(f"{API}/applications/{app_id}/permit")
    assert issued.status_code == 201, issued.text
    permit_id = uuid.UUID(issued.json()["id"])

    pdf_response = await journey_holder.client.get(f"{API}/permits/{permit_id}/pdf")
    assert pdf_response.status_code == 200, pdf_response.text
    pdf = pdf_response.content

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
    assert body["status"] == permits_service.PUBLIC_STATUS_LABELS["active"]

    # --- what only this test can see: the money is ONE number ----------------
    assert permit.amount == billed_amount, (
        f"the permit prints {permit.amount} but the citizen was billed {billed_amount}"
    )
    assert billed_calculation_id is not None
    assert str(permit.snapshot["calculation_id"]) == str(billed_calculation_id), (
        "the invoice and the permit's frozen snapshot name different calculations, "
        "so a paid invoice cannot be reconciled with the document it paid for"
    )


async def test_a_calculation_made_after_the_decision_refuses_issuance(
    db: AsyncSession,
    hodim_client: httpx.AsyncClient,
    approved_application: Application,
    grazing_activity_id: uuid.UUID,
):
    """A NEWER calculation lands between invoicing and issuance, and issuance
    is REFUSED rather than priced.

    Note what this does and does not prove. It asserts the refusal
    (`calculation_after_decision`), not a price — and it only fires because the
    test sets `decided_at` itself. Through all of 3.9a that column is NULL, so
    the divergence this describes is still uncaught today; the guard becomes
    live when 3.9b starts writing it.

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
    applicant_client: httpx.AsyncClient,
    journey_holder: Signer,
    approved_application: Application,
    grazing_activity_id: uuid.UUID,
):
    """**The guard this test used to stand in for now exists, and this asserts
    it.**

    `payments.issue_invoice` and `permits.issue` each read "the newest
    calculation for this application" independently. Nothing compares the two,
    and being both level 4 they cannot. A newer calculation attached to an
    application after it was invoiced bills the citizen 2 060 000,00 and prints
    9 999 999,00 on the permit — demonstrated by the test above, which has to
    reach past the API to build that state.

    Until stage 3.9a that was unreachable through the public write path only
    because `norms.schemas.CalculationIn.application_id` was typed
    `None = None`, so `POST /api/v1/calculations` could not bind a calculation
    to an application at all — an accident of 3.7's fail-closed edge (I4), not
    a designed guard. **3.9a task 5 opened the field and landed the real guards
    in the same commit**, in `norms.service.save_calculation` and not in
    `calc_router`, because `applications.service.submit` is the other caller
    and would walk straight past a router-level check.

    Both refusals are driven here through the REAL route — the one that
    requires `get_current_user` and no permission code at all, which is what
    made this a live money hole rather than a theoretical one:

      1. a STRANGER naming somebody else's application is told 404
         `ERR-SYS-003` — the same answer an id that never existed gets, never
         403, which would make this route an application-existence oracle for
         a document full of personal data;
      2. the application's OWN applicant is told 409 `ERR-NORM-005` because the
         application is APPROVED — at or beyond that line a price has been
         billed, and a newer row is the under-billing above.

    And nothing was written either time: `calculations` is append-only
    (migration 0011), so a row that slipped through could never be deleted or
    corrected.
    """
    period_from, period_to = approved_application.period_from, approved_application.period_to
    assert period_from is not None and period_to is not None
    body = {
        "application_id": str(approved_application.id),
        "contour_id": str(approved_application.contour_id),
        "activity_type_id": str(grazing_activity_id),
        "period_from": period_from.isoformat(),
        "period_to": period_to.isoformat(),
        "quantity": "1",
    }

    # The field itself now accepts a value — the accident is gone, and what
    # stands in its place is a guard rather than a type error.
    assert CalculationIn.model_fields["application_id"].annotation is not type(None)

    before = (
        await db.scalars(
            select(Calculation).where(Calculation.application_id == approved_application.id)
        )
    ).all()

    stranger = await applicant_client.post(f"{API}/calculations", json=body)
    assert stranger.status_code == 404, stranger.text
    assert stranger.json()["error"]["code"] == "ERR-SYS-003"

    owner = await journey_holder.client.post(f"{API}/calculations", json=body)
    assert owner.status_code == 409, owner.text
    error = owner.json()["error"]
    assert error["code"] == "ERR-NORM-005"
    assert error["details"]["reason"] == "application_closed_for_calculation"
    assert error["details"]["status"] == "APPROVED"

    after = (
        await db.scalars(
            select(Calculation).where(Calculation.application_id == approved_application.id)
        )
    ).all()
    assert [row.id for row in after] == [row.id for row in before], (
        "an append-only table: a row that slipped past the guard could never be removed"
    )


# --- task defect 5: gis's occupancy reader against TWO real permits ----------
#
# `gis.service.list_contours`/`contour_card` read `permits.occupancy_provider`
# (registered in `app/event_subscriptions.py`) for `s_available_ha`/
# `over_allocated`. Every existing test of that reading side fakes the
# provider (`tests/modules/gis/test_read_api.py`), and every existing test of
# the WRITING side (two applications, two permits) never reads gis back
# afterwards — so nothing had ever driven a contour through gis, applications,
# payments AND permits together and then asked gis what it now believes. This
# is exactly the seam the demo's own over-allocation defect lives on: two
# permits issued over one parcel, each perfectly legal on its own module's
# terms.


async def test_two_real_permits_on_one_contour_are_what_over_allocated_means(
    db: AsyncSession,
    hodim_client: httpx.AsyncClient,
    head_client: Signer,
    chief_forester_client: Signer,
    accountant_client: Signer,
    applicant_client: httpx.AsyncClient,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    grazing_activity_id: uuid.UUID,
):
    from datetime import date

    from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt
    from tests.modules.permits.conftest import (
        GRAZING_HERD,
        calculation_input_snapshot,
        make_permit_on_contour,
        unique_pinfl,
    )

    contour = await make_contour(db, contours_layer, leshoz)
    version = await make_version(
        db,
        contour.id,
        random_box_wkt(),
        status="published",
        approval_doc_id=approval_doc.id,
        area_ha=Decimal("50.0000"),
    )
    await db.commit()

    async def _paid_application_requesting(area_ha: Decimal) -> Application:
        user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
        applicant = Applicant(
            kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id
        )
        db.add(applicant)
        await db.flush()
        application = Application(
            applicant_id=applicant.id,
            submitted_by_user_id=user.id,
            on_behalf="self",
            activity_type_id=grazing_activity_id,
            contour_id=contour.id,
            contour_version_id=version.id,
            requested_area_ha=area_ha,
            period_from=date(2027, 5, 1),
            period_to=date(2027, 9, 30),
            status="APPROVED",
            channel="portal",
            assigned_org_id=leshoz.id,
        )
        db.add(application)
        await db.flush()
        db.add(
            Calculation(
                application_id=application.id,
                contour_id=contour.id,
                activity_type_id=grazing_activity_id,
                rule_code_version=RULE_CODE_VERSION,
                input_snapshot=calculation_input_snapshot(GRAZING_HERD),
                used_sb=Decimal("10.0000"),
                amount=Decimal("500000.00"),
                breakdown={"total": "500000.00"},
            )
        )
        await db.flush()
        await db.commit()
        return application

    async def _pay(application: Application) -> None:
        """Invoice the approved application and settle it — everything that
        must be true before issuance is even attempted. Split out of
        `_issue_and_activate` so the SECOND applicant can reach the issuance
        gate genuinely paid: refused there for the right reason (the slot is
        gone) rather than for the wrong one (ERR-PAY-001, not paid)."""
        app_id = application.id
        await publish(db, Event(name=APPLICATION_APPROVED, payload={"application_id": app_id}))
        await db.commit()
        invoice = await payments_service.invoice_for_application(db, app_id)
        assert invoice is not None
        await db.refresh(invoice)
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

    async def _issue_and_activate(application: Application) -> None:
        app_id = application.id
        await _pay(application)

        issued = await hodim_client.post(f"{API}/applications/{app_id}/permit")
        assert issued.status_code == 201, issued.text
        permit_id = uuid.UUID(issued.json()["id"])
        pdf = (await accountant_client.client.get(f"{API}/permits/{permit_id}/pdf")).content

        applicant_row = await db.get(Applicant, application.applicant_id)
        assert applicant_row is not None and applicant_row.owner_user_id is not None
        holder_user = await db.get(User, applicant_row.owner_user_id)
        assert holder_user is not None
        async for holder in _signer_for(db, role_code="applicant", user=holder_user):
            for signer, purpose in (
                (head_client, "permit_head"),
                (chief_forester_client, "permit_chief_forester"),
                (accountant_client, "permit_accountant"),
                (holder, signers.RECIPIENT_PURPOSE),
            ):
                result = await sign_permit(signer, permit_id, purpose, pdf)
                assert result.status_code == 200, (purpose, result.text)

    # Two independently unremarkable applications — 30 + 30 ga on a 50 ga
    # contour. Neither the applications module, the payments module nor the
    # permits module has any reason to refuse either one: each is priced,
    # paid and signed on its own terms, and no single module reads the
    # other's allocation at all (module boundary rule).
    first = await _paid_application_requesting(Decimal("30.0000"))
    second = await _paid_application_requesting(Decimal("30.0000"))
    await _issue_and_activate(first)

    # SINCE RULING #176 THE SECOND ISSUANCE IS REFUSED — this is the defect
    # above, fixed. The contour carries no norm and therefore no capacity, so
    # it is EXCLUSIVE for the period, and the second paid, approved, perfectly
    # legal-on-its-own-terms application cannot become a permit over the same
    # plot and dates. That refusal is what this half now pins; the applicant
    # has paid, and the money question is a refund, not a second document.
    await _pay(second)
    refused = await hodim_client.post(f"{API}/applications/{second.id}/permit")
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "ERR-NORM-002"
    assert error["details"]["reason"] == "exclusive_occupied"

    # The gis reading side still has to be exercised against a genuinely
    # over-allocated contour, because databases predating this stage contain
    # exactly that: two active permits on one parcel, issued when nothing
    # refused them. Built through the ORM, since issuance — correctly — will
    # not produce one any more.
    legacy = await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version.id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="active",
        area_ha=Decimal("30.0000"),
        period_from=date(2027, 5, 1),
        period_to=date(2027, 9, 30),
    )
    assert legacy.status == "active"
    await db.commit()

    card = await applicant_client.get(f"{API}/gis/contours/{contour.id}")
    assert card.status_code == 200, card.text
    body = card.json()
    assert body["occupancy_source"] == "permits"
    assert body["occupied_ha"] == "60.0000", (
        "permits.occupancy_provider should count both active permits — the one "
        "issued through the real flow and the legacy one — if this drops to "
        "30, the seam between issuance and the occupancy provider broke, not "
        "gis's own arithmetic"
    )
    assert body["s_available_ha"] == "0", "defect 2's floor: never a negative available area"
    assert body["over_allocated"] is True, (
        "defect 2's explicit signal: 60 ga committed on a 50 ga contour is a "
        "real over-allocation across two active permits, not a fabricated "
        "provider in a unit test. Ruling #176 stops NEW ones being created; "
        "this signal is how the ones already in the database stay visible"
    )


# --- Stage 10: the free path — a 100% benefit, the button, self-settlement ---


async def test_the_free_path_from_filing_to_an_active_permit(
    db: AsyncSession,
    head_client: Signer,
    chief_forester_client: Signer,
    accountant_client: Signer,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
) -> None:
    """Rulings #181-#185, walked together end to end — each was proven inside
    its own module's suite (`applications/test_submit.py`,
    `applications/test_benefit_verification.py`, `payments/
    test_free_settlement.py`, `permits/test_signatures.py`), and nothing
    before this test ran the whole chain: a citizen filing WITH the button
    (#183), a 100% recreation benefit the leshoz verifies (#181/#182), an
    invoice that settles ITSELF with no Payme step at all (#185), and the
    holder signing the printed permit with the SAME button (#183) rather
    than an ERI.

    The tariff mutated below is migration `0012`'s own seeded, published
    `recreation` row — shared, singleton data `tests/modules/norms/
    test_seeds.py` asserts a coefficient against but never `benefit_
    modifiers` — restored in `finally`, the same "own session, undo it"
    shape `test_submit.py::
    test_a_tariff_published_between_package_and_submit_is_also_package_changed`
    already uses for the identical class of row.
    """
    from datetime import date

    from sqlalchemy import text

    from app.core import storage
    from app.modules.applications.models import ApplicationStatusHistory
    from app.modules.integrations.adapters.eimzo import encode_mock_signature
    from app.modules.norms.models import Tariff
    from app.modules.permits.models import PermitTemplate
    from app.modules.signatures.models import Signature
    from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt
    from tests.modules.permits.conftest import _signer_for, sign_permit_simple, unique_pinfl

    BENEFIT_CODE = "war_veterans"  # one of #181's six recreation categories

    contour = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    await db.commit()

    recreation_activity_id = await db.scalar(
        text("SELECT id FROM activity_types WHERE code = 'recreation'")
    )
    benefit_category_item_id = await db.scalar(
        text(
            "SELECT i.id FROM classifier_items i JOIN classifiers c ON c.id = i.classifier_id "
            "WHERE c.code = 'benefit_categories' AND i.code = :code AND i.status = 'active'"
        ).bindparams(code=BENEFIT_CODE)
    )
    benefit_doc_type_item_id = await db.scalar(
        text(
            "SELECT i.id FROM classifier_items i JOIN classifiers c ON c.id = i.classifier_id "
            "WHERE c.code = 'doc_types' AND i.code = 'benefit_proof' AND i.status = 'active'"
        )
    )
    assert recreation_activity_id is not None
    assert benefit_category_item_id is not None
    assert benefit_doc_type_item_id is not None, "migration 0024 seeds this; go-live checklist item"

    tariff = (
        await db.execute(
            select(Tariff).where(
                Tariff.activity_type_id == recreation_activity_id,
                Tariff.livestock_group.is_(None),
                Tariff.status == "published",
            )
        )
    ).scalar_one()
    original_modifiers = tariff.benefit_modifiers
    tariff.benefit_modifiers = {BENEFIT_CODE: "0"}
    await db.commit()

    # Migration 0019 seeds an active template for `grazing` alone — every
    # other activity has none until an administrator uploads one
    # (`permits/models.py::PermitTemplate`'s own docstring), and the layout
    # BUNDLED with the module (`layout_file_id=NULL`) is grazing's own —
    # `{{sb_load}}` among its placeholders, which a recreation application
    # never fills (`ERR-VAL-001 unfilled_placeholders`). A STORED layout of
    # this test's own, the same shape `permits/conftest.py::apiary_template`
    # uses, sidesteps every activity-specific placeholder.
    RECREATION_LAYOUT_FILE_ID = uuid.UUID("01a06200-0000-7000-8000-000000000002")
    RECREATION_LAYOUT_KEY = "t/permits-recreation-layout-b2.html"
    layout_bytes = (
        "<html><body><h1>{{ series }} № {{ number }}</h1>"
        '<p>Recreation — {{ holder_name }}</p><img src="{{ qr }}"></body></html>'
    ).encode()
    await storage.ensure_bucket()
    await storage.put_object(RECREATION_LAYOUT_KEY, layout_bytes, "text/html")
    layout_file = await db.get(MediaFile, RECREATION_LAYOUT_FILE_ID)
    if layout_file is None:
        layout_file = MediaFile(
            id=RECREATION_LAYOUT_FILE_ID,
            storage_key=RECREATION_LAYOUT_KEY,
            filename="recreation_layout.html",
            content_type="text/html",
            size_bytes=len(layout_bytes),
            sha256=hashlib.sha256(layout_bytes).hexdigest(),
        )
        db.add(layout_file)
        await db.flush()

    # Get-or-create: a previous run's row is reused rather than colliding
    # with `uq_permit_templates_active`.
    recreation_template = (
        await db.execute(
            select(PermitTemplate).where(
                PermitTemplate.activity_type_id == recreation_activity_id,
                PermitTemplate.status == "active",
            )
        )
    ).scalar_one_or_none()
    if recreation_template is None:
        recreation_template = PermitTemplate(
            activity_type_id=recreation_activity_id,
            version=1,
            name={"uz_cyrl": "Тест бланки", "en": "Test recreation layout"},
            layout_file_id=layout_file.id,
            status="active",
            valid_from=date(2020, 1, 1),
        )
        db.add(recreation_template)
    recreation_template.layout_file_id = layout_file.id
    await db.commit()

    applicant_user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    db.add(
        Applicant(
            kind="individual",
            pinfl=applicant_user.pinfl,
            name=applicant_user.full_name,
            owner_user_id=applicant_user.id,
            address="Toshkent shahri, Chilonzor tumani, 1-uy",
        )
    )
    await db.flush()
    await db.commit()

    try:
        async for reviewer in _signer_for(
            db, role_code="executor_staff", organization_id=leshoz.id
        ):
            async for citizen in _signer_for(db, role_code="applicant", user=applicant_user):
                # --- file: recreation, self, a real 100% benefit claim ------------
                created = await citizen.client.post(
                    f"{API}/applications", json={"on_behalf": "self"}
                )
                assert created.status_code == 201, created.text
                app_id = created.json()["id"]

                patched = await citizen.client.patch(
                    f"{API}/applications/{app_id}",
                    json={
                        "contour_id": str(contour.id),
                        "activity_type_id": str(recreation_activity_id),
                        "period_from": "2027-09-01",
                        "period_to": "2027-09-30",
                        "quantity": "2",
                        "benefit_category_item_id": str(benefit_category_item_id),
                        "benefit_certificate_no": "VET-0001",
                    },
                )
                assert patched.status_code == 200, patched.text

                uploaded = await citizen.client.post(
                    f"{API}/files",
                    files={"file": ("proof.pdf", b"%PDF-1.4 test", "application/pdf")},
                )
                assert uploaded.status_code == 201, uploaded.text
                proof = await citizen.client.post(
                    f"{API}/applications/{app_id}/documents",
                    json={
                        "doc_type_item_id": str(benefit_doc_type_item_id),
                        "file_id": uploaded.json()["id"],
                    },
                )
                assert proof.status_code == 201, proof.text

                # --- the button (ruling #183): on_behalf=self, no envelope -------
                submitted = await citizen.client.post(
                    f"{API}/applications/{app_id}/submit",
                    json={"rules_accepted": True},
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                )
                assert submitted.status_code == 200, submitted.text
                assert submitted.json()["benefit_verification_status"] == "pending"
                assert submitted.json()["rules_accepted_at"] is not None

                history = (
                    await db.scalars(
                        select(ApplicationStatusHistory).where(
                            ApplicationStatusHistory.application_id == uuid.UUID(app_id),
                            ApplicationStatusHistory.to_status == "SUBMITTED",
                        )
                    )
                ).one()
                submission_signature = (
                    await db.scalars(
                        select(Signature).where(
                            Signature.object_type == "application_submission",
                            Signature.object_id == history.id,
                        )
                    )
                ).one()
                assert submission_signature.kind == "simple"

                # --- the leshoz's own review: start, then verify the claim --------
                started = await reviewer.client.post(f"{API}/applications/{app_id}/start-review")
                assert started.status_code == 200, started.text
                verified = await reviewer.client.post(
                    f"{API}/applications/benefit-verifications/{app_id}/verify"
                )
                assert verified.status_code == 200, verified.text
                assert verified.json()["benefit_verification_status"] == "verified"

                # --- the head approves; the invoice must settle ITSELF -------------
                doc = (await head_client.client.get(f"{API}/applications/{app_id}/package")).content
                approved = await head_client.client.post(
                    f"{API}/applications/{app_id}/approve",
                    json={
                        "pkcs7": encode_mock_signature(
                            document=doc,
                            serial=head_client.serial,
                            issuer="ISS-1",
                            pinfl=head_client.pinfl,
                        )
                    },
                )
                assert approved.status_code == 200, approved.text
                # NOT "INVOICED" — ruling #185's `_settle_free` runs INSIDE this
                # same request's transaction, so a 100% benefit claim is already
                # PAID by the time this response serializes; the ordinary
                # (still-to-be-paid) case is what every OTHER decision test in
                # this file asserts "INVOICED" against.
                assert approved.json()["status"] == "PAID"

                application = await applications_service.get(db, uuid.UUID(app_id))
                assert application is not None
                assert (await _reread(db, application)).status == "PAID", (
                    "ruling #185: a benefit-settled invoice moves the application "
                    "straight to PAID — no Payme step, no manual confirmation"
                )

                invoice = await payments_service.invoice_for_application(db, application.id)
                assert invoice is not None
                await db.refresh(invoice)
                assert invoice.status == "paid"
                assert invoice.amount == Decimal("0.00")
                assert await payments_service.is_settled_without_payment(db, invoice) is True

                provider_txns = await db.scalars(
                    select(ProviderTransaction).where(ProviderTransaction.invoice_id == invoice.id)
                )
                assert list(provider_txns) == [], "ruling #185: nothing arrived, nothing to split"

                # --- a human forms the document -----------------------------------
                issued = await reviewer.client.post(f"{API}/applications/{app_id}/permit")
                assert issued.status_code == 201, issued.text
                permit_id = uuid.UUID(issued.json()["id"])

                pdf_response = await citizen.client.get(f"{API}/permits/{permit_id}/pdf")
                assert pdf_response.status_code == 200, pdf_response.text
                pdf = pdf_response.content
                assert len(pdf) > 0

                # --- the 3+1 signatures: the holder uses the button (ruling #183) -
                for signer, purpose in (
                    (head_client, "permit_head"),
                    (chief_forester_client, "permit_chief_forester"),
                    (accountant_client, "permit_accountant"),
                ):
                    result = await sign_permit(signer, permit_id, purpose, pdf)
                    assert result.status_code == 200, (purpose, result.text)
                holder_result = await sign_permit_simple(
                    citizen, permit_id, signers.RECIPIENT_PURPOSE
                )
                assert holder_result.status_code == 200, holder_result.text

                permit = await permits_service.get(db, permit_id)
                assert permit is not None
                assert (await _reread(db, permit)).status == "active"
                assert (await _reread(db, application)).status == "PERMIT_ISSUED"

                holder_signature = (
                    await db.scalars(
                        select(Signature).where(
                            Signature.object_type == "permit",
                            Signature.object_id == permit_id,
                            Signature.purpose == signers.RECIPIENT_PURPOSE,
                        )
                    )
                ).one()
                assert holder_signature.kind == "simple"
                assert holder_signature.certificate_id is None
    finally:
        tariff.benefit_modifiers = original_modifiers
        await db.commit()
