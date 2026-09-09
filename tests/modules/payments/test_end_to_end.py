"""One end-to-end payment run, and the public surface Task 7 freezes for
3.11 `permits` (design/02 § payments, plan `03.10a-payments-core` task 7).

The brief's own two tests, translated per two standing rulings:

- **`applications` ships no HTTP surface on this branch.** The brief's own
  `approved_via_api` fixture drove a submit -> approve HTTP flow that does
  not exist here (3.9a branch 1 only — same gap `test_invoice.py`,
  `test_expiry.py` and `test_payme_rpc.py` were already adapted for). Below,
  `approved_via_api` reaches the SAME real state instead — an application
  carried from APPROVED to INVOICED by a genuine `APPLICATION_APPROVED`
  publish — through this package's own `pending_invoice` fixture
  (`conftest.py`), which does exactly that. The brief's own
  `applicant_client.get(f"/api/v1/applications/{app_id}")` card check is
  replaced by `applications.service.get`, the same cross-module read
  `test_invoice.py`/`test_expiry.py` already use for the identical purpose.
- **The brief's own `applicant_client` is this package's STRANGER fixture**
  (`conftest.py`'s own docstring: "unrelated to `approved_application`'s own
  applicant"), so posting a pay-intent as `applicant_client` would 404
  rather than the 201 the brief's test expects. `test_intents.py` hit the
  identical naming collision and resolved it with a file-local
  `applicant_client` fixture forwarding `owner_client` — copied verbatim
  below, so the brief's test body needs no further change.

Every Payme transaction id below comes from `test_payme_rpc.py`'s own
`_tx_id()` (never a fixed literal — the shared, persistent test DB lesson);
the brief's own hard-coded `"e2e-tx"` is replaced the same way.

Plus two coverage-gap tests (review rulings, not in the brief): every
existing fixture in this package builds an application with no `contour_id`
and no `assigned_org_id`, so `confirm_payment`'s recipient-account chain
(Task 4 ruling H) has never actually run, and nothing pins the ROLLBACK half
of Task 4's atomicity claim (every other test's `PerformTransaction`
succeeds).
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, publish
from app.modules.admin.models import Organization
from app.modules.applications.events import APPLICATION_APPROVED
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant
from app.modules.gis.models import Contour
from app.modules.norms.models import Calculation
from app.modules.payments import service as payments_service
from app.modules.payments.models import Allocation, Invoice, ProviderTransaction
from tests.modules.applications.conftest import published_contour as published_contour
from tests.modules.gis.conftest import approval_doc as approval_doc
from tests.modules.gis.conftest import contours_layer as contours_layer
from tests.modules.gis.conftest import leshoz as leshoz

# --- fixtures ----------------------------------------------------------------
#
# `published_contour` (applications/conftest.py) itself depends on
# `contours_layer`/`leshoz`/`approval_doc` (gis/conftest.py) — pytest
# resolves a fixture's own parameters against the CURRENT test's fixture
# closure, not the file the fixture happens to be defined in, so all four
# names need re-exporting here the same way applications/conftest.py itself
# had to re-export gis's four primitives to make its own `published_contour`
# usable at all. `payments/conftest.py` re-exports neither `published_contour`
# nor `leshoz` (every existing payments fixture is contour-free), so this
# file adds its own.


@pytest.fixture
def applicant_client(owner_client):
    """This file's own vocabulary, copied from `test_intents.py`'s own fix
    for the identical naming collision: the brief's test below calls the
    invoice OWNER's client `applicant_client`, but `conftest.py`'s own
    `applicant_client` (re-exported from `gis.conftest`) is a STRANGER —
    `owner_client` (`conftest.py`) is the same concept under the name
    `test_invoice.py`'s 'owner vs stranger' tests use instead."""
    return owner_client


@pytest.fixture
async def approved_via_api(
    approved_application: Application, pending_invoice: Invoice
) -> tuple[uuid.UUID, Decimal]:
    """Ruling translation of the brief's own `approved_via_api` — see the
    module docstring. `pending_invoice` (`conftest.py`) already carries
    `approved_application` from APPROVED to INVOICED through a real
    `APPLICATION_APPROVED` publish, the same event a future submit->approve
    HTTP flow will publish; this fixture only reshapes that into the
    `(application_id, amount)` tuple the brief's own test destructures.
    `amount` is read off the invoice `pending_invoice` itself built — the
    same frozen `Calculation.amount` `issue_invoice` charged — never
    re-hardcoded, so this cannot silently drift from what `approved_application`
    actually bills."""
    return approved_application.id, pending_invoice.amount


@pytest.fixture
async def application_with_org_account(
    db: AsyncSession,
    applicant: Applicant,
    grazing_activity_id: uuid.UUID,
    published_contour: Contour,
    leshoz: Organization,
) -> Application:
    """Closes the recipient-account coverage gap (review ruling): every
    OTHER application fixture in this package carries no `contour_id` and no
    `assigned_org_id`, so `confirm_payment`'s `_resolve_recipient_account`
    (Task 4 ruling H: `contour_id` -> `gis.service.contour_organization` ->
    `admin.repo.get_organization` -> `organization.requisites["account"]`)
    always short-circuits to `None` and that whole chain has never executed.

    `published_contour` (`tests/modules/applications/conftest.py`) is filed
    under `leshoz` (`tests/modules/gis/conftest.py`'s own `make_contour` sets
    `organization_id=org.id`) — giving `leshoz` a real bank account directly
    on its own `requisites` JSONB, then pointing an application's
    `contour_id` at that same contour, exercises the full chain for real."""
    leshoz.requisites = {"account": "20208000123456789012"}
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
            input_snapshot={},
            amount=Decimal("150000.00"),
            breakdown={},
        )
    )
    await db.flush()
    return application


@pytest.fixture
async def pending_invoice_with_org_account(
    db: AsyncSession, application_with_org_account: Application
) -> Invoice:
    """`application_with_org_account`, carried to INVOICED the same real way
    `conftest.py`'s own `pending_invoice` carries `approved_application` —
    a genuine `APPLICATION_APPROVED` publish, not a hand-set status."""
    await publish(
        db,
        Event(
            name=APPLICATION_APPROVED,
            payload={"application_id": application_with_org_account.id},
        ),
    )
    await db.commit()
    invoice = await payments_service.invoice_for_application(db, application_with_org_account.id)
    assert invoice is not None
    await db.refresh(invoice)
    return invoice


# --- tests --------------------------------------------------------------------


async def test_an_approved_application_is_paid_end_to_end(
    db, client, applicant_client, approved_via_api
):
    """APPROVED -> INVOICED -> checkout -> Payme performs -> PAID, with the
    ledger balanced. If this passes, 3.11 has everything it needs.

    Calls `service.invoice_for_application`, `service.is_paid` and
    `service.allocations_for` directly, in-process, off the SAME fixtures
    the HTTP path above just proved — the lesson this whole task is built
    on ('A "public surface" task's own end-to-end test can ship the surface
    untested'): an HTTP scenario alone would exercise the ROUTES, never the
    in-process functions 3.11 actually calls.
    """
    from app.modules.applications import service as applications_service
    from app.modules.payments import service
    from tests.modules.payments.test_payme_rpc import _rpc, _tx_id

    app_id, amount = approved_via_api

    invoice = await service.invoice_for_application(db, app_id)
    assert invoice is not None
    assert invoice.status == "pending"

    intent = await applicant_client.post(
        f"/api/v1/invoices/{invoice.id}/pay-intents",
        json={"provider": "payme"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert intent.status_code == 201, intent.text

    tx_id = _tx_id("e2e")
    params = {
        "id": tx_id,
        "time": 1_800_000_000_000,
        "amount": int(amount * 100),
        "account": {"id": invoice.number},
    }
    await _rpc(client, "CreateTransaction", params)
    performed = await _rpc(client, "PerformTransaction", {"id": tx_id})
    assert performed.json()["result"]["state"] == 2

    # `invoice` was loaded into `db`'s identity map by the FIRST
    # `invoice_for_application` call above, before the HTTP round-trip paid
    # it through a DIFFERENT session — without this refresh, `is_paid`'s own
    # fresh SELECT still merges into that same, now-stale Python object
    # rather than overwriting it (lesson: "The row in memory is not what
    # Postgres stored"; same reasoning as the `db.refresh(application)`
    # below, and `test_payme_rpc.py`'s own `db.refresh(pending_invoice)`).
    await db.refresh(invoice)
    assert await service.is_paid(db, app_id) is True

    entries = await service.allocations_for(db, invoice.id)
    assert sum(e.amount for e in entries) == amount

    application = await applications_service.get(db, app_id)
    assert application is not None
    await db.refresh(application)
    assert application.status == "PAID"


async def test_is_paid_is_false_for_an_invoiced_but_unpaid_application(db, approved_via_api):
    """The guard 3.11 depends on: tz/04 С11 — no payment, no permit, and an
    attempt raises RI-10 (3.11's own job — this only proves the guard itself
    reports `False` before any payment has landed)."""
    from app.modules.payments import service

    app_id, _ = approved_via_api
    assert await service.is_paid(db, app_id) is False


async def test_perform_transaction_resolves_the_recipient_account_through_the_contour(
    db, client, pending_invoice_with_org_account
):
    """Closes the coverage gap: `allocations.account` is the entire point of
    the ledger, and until now nothing had ever driven `confirm_payment`'s
    recipient-account chain past its own `None` short-circuit."""
    from tests.modules.payments.test_payme_rpc import _rpc, _tx_id

    invoice = pending_invoice_with_org_account
    tx_id = _tx_id("recipient-account")
    params = {
        "id": tx_id,
        "time": 1_800_000_000_000,
        "amount": int(invoice.amount * 100),
        "account": {"id": invoice.number},
    }
    await _rpc(client, "CreateTransaction", params)
    performed = await _rpc(client, "PerformTransaction", {"id": tx_id})
    assert performed.json()["result"]["state"] == 2

    entries = (
        await db.scalars(select(Allocation).where(Allocation.invoice_id == invoice.id))
    ).all()
    by_target = {e.target: e for e in entries}
    assert by_target["recipient"].account == "20208000123456789012"
    # Stage 7.9 task 5: the seeded `budget_50` directory row (migration
    # `0045`, always active in a fresh test DB) is now a configured
    # RECEIVER, not the old engine's fixed "budget" half — its own account
    # stays `None` regardless (Override 5: a directory row names a Payme
    # wallet, never a bank account).
    assert by_target["receiver"].account is None


async def test_a_failure_inside_perform_transaction_rolls_back_the_whole_write(
    db, client, pending_invoice, monkeypatch
):
    """Closes the coverage gap: nothing pins the ROLLBACK half of Task 4's
    atomicity claim — every other test's `PerformTransaction` succeeds.
    Forces `applications.service.set_status` (`confirm_payment`'s own last
    write) to raise, and proves `payme_router._process`'s generic `except
    Exception` branch (module docstring's 'Commit discipline') really
    discards everything `confirm_payment` staged before that point: the
    `invoice.status = "paid"` assignment, the two flushed `Allocation`
    rows, and `transaction.state = STATE_PERFORMED` — not just the
    application transition itself."""
    from app.modules.applications import service as applications_service
    from tests.modules.payments.test_payme_rpc import _rpc, _tx_id

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("forced failure for the rollback test")

    tx_id = _tx_id("rollback")
    params = {
        "id": tx_id,
        "time": 1_800_000_000_000,
        "amount": int(pending_invoice.amount * 100),
        "account": {"id": pending_invoice.number},
    }
    await _rpc(client, "CreateTransaction", params)

    monkeypatch.setattr(applications_service, "set_status", _boom)
    performed = await _rpc(client, "PerformTransaction", {"id": tx_id})

    assert performed.status_code == 200
    assert performed.json()["error"]["code"] == -32400

    await db.refresh(pending_invoice)
    assert pending_invoice.status == "pending"

    transaction = (
        await db.execute(
            select(ProviderTransaction).where(
                ProviderTransaction.provider == "payme", ProviderTransaction.external_id == tx_id
            )
        )
    ).scalar_one()
    assert transaction.state == "1"

    count = await db.scalar(
        select(func.count())
        .select_from(Allocation)
        .where(Allocation.invoice_id == pending_invoice.id)
    )
    assert count == 0


async def test_payment_confirmed_carries_both_the_invoice_and_the_application_id(
    db, client, pending_invoice
):
    """Whole-branch review. The frozen public surface takes an
    `application_id` in both directions (`invoice_for_application`,
    `is_paid`) and forbids a level-4+ caller from reading `invoices` as a
    table — so an `invoice_id`-only payload gives a subscriber no way to
    reach anything at all. 3.11 `permits`'s subscriber reads
    `application_id` off this event and returns early without it, logging a
    warning: dropping the key would silently stop every permit from being
    raised, forever, with nothing failing anywhere.

    Asserts the KEYS as well as the values: an extra key is tolerable, a
    missing one is the defect."""
    from app.core import events
    from app.modules.payments import events as payment_events
    from tests.modules.payments.test_payme_rpc import _rpc, _tx_id

    seen: list[events.Event] = []

    async def _spy(_db: AsyncSession, event: events.Event) -> None:
        seen.append(event)

    # Removed for the next test by the root conftest's `_isolate_subscriptions`
    # (it restores the snapshot taken before this test ran) — the bus is
    # process-global and a spy left behind would fire three files away.
    events.subscribe(payment_events.PAYMENT_CONFIRMED, _spy)

    tx_id = _tx_id("payload")
    params = {
        "id": tx_id,
        "time": 1_800_000_000_000,
        "amount": int(pending_invoice.amount * 100),
        "account": {"id": pending_invoice.number},
    }
    await _rpc(client, "CreateTransaction", params)
    performed = await _rpc(client, "PerformTransaction", {"id": tx_id})
    assert performed.json()["result"]["state"] == 2

    assert len(seen) == 1, "confirm_payment publishes payment_confirmed exactly once"
    payload = seen[0].payload
    assert set(payload) >= {"invoice_id", "application_id"}
    assert payload["invoice_id"] == str(pending_invoice.id)
    assert payload["application_id"] == str(pending_invoice.application_id)
