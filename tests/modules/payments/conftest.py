"""Fixtures for the payments module.

`applications` has no HTTP surface yet on `dev` (3.9a branch 1's own conftest note),
so an application row is built directly through the ORM — the same idiom
`tests/modules/applications/test_public_surface.py`'s `_draft` helper uses, with
`status` forced to APPROVED: payments only needs the row SHAPE an approved
application has, not a real transition (history row, audit entry) to reach it —
that stays branch 2's job.

`applicant` is re-exported from `tests.modules.applications.conftest` rather than
rebuilt here — the same cross-module fixture reuse `tests/modules/norms/conftest.py`
already does for gis's fixtures.

Task 2 adds: a saved `Calculation` on `approved_application` (task-2 ruling P4 —
`issue_invoice` must have something real to bill), the sibling
`approved_without_calculation` for the loud-failure test, and the HTTP client
fixtures the new `/invoices/*` read routes need (`payments` has no router.py
before Task 2, so this package's own `_app_on_test_db` guard is new too — lesson:
"A module's test conftest needs plumbing copied from an existing one").

Task 4 adds: `client` (an ANONYMOUS httpx client for the Payme JSON-RPC endpoint
— Basic auth via a request header, never a cookie session, mirrors
`notifications/test_eskiz_callback.py`'s own anonymous-provider-route client),
`pending_invoice`/`cancelled_invoice` (ruling F: the latter is a `pending_invoice`
cancelled through the REAL path — publishing `applications.events.
APPLICATION_CANCELLED` on the bus, exactly what `payments.subscribers.
on_application_cancelled` is wired to react to — never a row constructed with
`status="cancelled"` directly, which would prove nothing about the subscriber
chain), and `frozen_clock` (ruling G: patches `payme_router._now`, the ONE
module-level clock helper the route reads `now` through — never `app.core.time`,
which that route has no reason to use at all). `_app_on_test_db` additionally
sets `PAYME_CASHBOX_KEY` so the module's own hardcoded test key
(`test-cashbox-key`, matching `test_payme_rpc.py`'s own `_auth()` default)
authenticates against something real.

Task 3 (recipients directory) reuses `client` verbatim for
`test_recipients_api.py` too — it was already the plain anonymous shape that
test file needs, since every one of its requests supplies its own actor via
`headers=` instead of a cookie the client itself carries (several of its
tests compare what TWO different actors may do against the SAME route, which
a single signed-in client like `payments_view_client` below cannot express).
It adds `sys_admin`/`accountant` (headers for those two actors) and
`budget_50`/`budget_50_inactive` (the ONE `payment_recipients` row migration
`0045` seeds, read rather than duplicated — Override 3 of that task's own
brief)."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.events import Event, publish
from app.db import make_session_factory
from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.applications.events import APPLICATION_APPROVED, APPLICATION_CANCELLED
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, User
from app.modules.norms.models import Calculation
from app.modules.payments import payme_router
from app.modules.payments import service as payments_service
from app.modules.payments.models import Invoice, PaymentRecipient
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.applications.conftest import applicant as applicant
from tests.modules.applications.conftest import grazing_activity_id as grazing_activity_id
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.gis.conftest import _commit_pending_before_requests
from tests.modules.gis.conftest import applicant_client as applicant_client
from tests.modules.gis.conftest import leshoz as leshoz

PAYME_TEST_CASHBOX_KEY = "test-cashbox-key"
# Task 5: `build_checkout_url` reads `payme_merchant_id` for real in BOTH
# modes (ruling — nothing about it is mock-sensitive), so the test config
# needs one set for `test_intents.py`'s determinism to come from anywhere.
PAYME_TEST_MERCHANT_ID = "test-merchant-id"

# Mirrors migrations/versions/0045_payment_split.py::BUDGET_RECIPIENT_ID. That
# module's name starts with a digit and cannot be imported (`import
# 0045_payment_split` is a SyntaxError), so the literal is duplicated here —
# the same idiom tests/modules/permits/conftest.py uses for
# APIARY_LAYOUT_FILE_ID, a seeded row's id it cannot import either.
BUDGET_RECIPIENT_ID = uuid.UUID("0192f2a0-0000-7000-8000-000000000001")

# Stage 7.9 task 6 (decision #160): fixed TEST-ONLY Payme account ids —
# migration `0045`'s own seed comment is explicit that the real one is
# "to be filled in by the Agency" (decision #159, still open), so these
# stand in for it. `_budget_recipient_is_routable` (below) writes the first
# onto the seeded `budget_50` row; `pending_invoice` writes the second onto
# ITS OWN fresh `leshoz` — see each fixture's own docstring for why both are
# needed for `payme.py`'s new `-31008` routability check to leave every
# PRE-EXISTING fixture in this package payable, exactly as it was before
# this task.
PAYME_TEST_BUDGET_ACCOUNT_ID = "test-budget-payme-id"
PAYME_TEST_LESHOZ_ACCOUNT_ID = "test-leshoz-payme-id"


async def _new_approved_application(db: AsyncSession, applicant: Applicant) -> Application:
    row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
        status="APPROVED",
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def approved_application(
    db: AsyncSession, applicant: Applicant, grazing_activity_id: uuid.UUID
) -> Application:
    """An application already in APPROVED status, WITH a saved `Calculation`
    carrying a known amount — what `issue_invoice` bills from (task-2 ruling
    P4: 'a saved norms.models.Calculation row carrying a known amount').
    Extended from Task 1's own fixture, which had no calculation at all."""
    row = await _new_approved_application(db, applicant)
    db.add(
        Calculation(
            application_id=row.id,
            activity_type_id=grazing_activity_id,
            rule_code_version="norms-1.0.0",
            input_snapshot={},
            amount=Decimal("150000.00"),
            breakdown={},
        )
    )
    await db.flush()
    return row


@pytest.fixture
async def approved_without_calculation(db: AsyncSession, applicant: Applicant) -> Application:
    """An APPROVED application with NO calculation row — `issue_invoice`'s
    required failure mode: a missing calculation must fail loudly (a mapped
    `DomainError`), never produce a silent zero-amount invoice."""
    return await _new_approved_application(db, applicant)


@pytest.fixture
async def invoice(db: AsyncSession, approved_application: Application) -> Invoice:
    """A single pending invoice for `approved_application` — what the third brief
    test's `ProviderTransaction` rows point `invoice_id` at (ruling P1)."""
    row = Invoice(
        application_id=approved_application.id,
        number=f"INV-2027-{uuid.uuid4().hex[:6]}",
        amount=Decimal("100.00"),
        status="pending",
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def pending_invoice(
    db: AsyncSession, approved_application: Application, leshoz: Organization
) -> Invoice:
    """A pending invoice produced by the REAL path (coordinator finding,
    task 4): publishing `applications.events.APPLICATION_APPROVED` on the
    bus, which `payments.subscribers.on_application_approved` turns into
    `service.issue_invoice` — the same call that freezes `approved_application`'s
    own `Calculation` into an invoice AND moves the application APPROVED ->
    INVOICED. A fixture that hand-wrote `status="INVOICED"` on the
    application (the first version of this fixture did exactly that) would
    hide a regression in that exact transition: `APPROVED -> PAID` is not a
    legal jump (`APPLICATION_TRANSITIONS`), so `payme.PerformTransaction`'s
    own `set_status(..., "PAID")` call correctly refused it, and only
    building the precondition through the real code under test caught that
    this fixture — not the implementation — was wrong.

    Stage 7.9 task 6 (decision #160): also points `approved_application` at
    `leshoz` (`tests/modules/gis/conftest.py`, a FRESH organization —
    function-scoped, so mutating it here touches nothing outside this one
    test), carrying a Payme account id on its own `requisites` — the same
    idiom `test_invoice_snapshot.py::leshoz_with_payme_id` uses. Without
    this, the invoice's own remainder row (the leshoz's half of the seeded
    `budget_50`'s split — its OWN half is made routable module-wide by
    `_budget_recipient_is_routable` below) would still carry no Payme id,
    and `payme.py`'s new `CheckPerformTransaction`/`CreateTransaction`
    refusal (`-31008`) would make CreateTransaction fail for every one of
    this fixture's many consumers across the package, none of which are
    about testing that refusal (`test_payme_receivers.py` alone is, with
    its own deliberately-unrouted fixtures)."""
    leshoz.requisites = {"payme_account_id": PAYME_TEST_LESHOZ_ACCOUNT_ID}
    approved_application.assigned_org_id = leshoz.id
    await publish(
        db,
        Event(name=APPLICATION_APPROVED, payload={"application_id": approved_application.id}),
    )
    await db.commit()
    row = await payments_service.invoice_for_application(db, approved_application.id)
    assert row is not None
    await db.refresh(row)
    return row


@pytest.fixture
async def cancelled_invoice(db: AsyncSession, pending_invoice: Invoice) -> Invoice:
    """A `pending_invoice` cancelled through the REAL path (ruling F):
    publishing `applications.events.APPLICATION_CANCELLED` on the bus, which
    `payments.subscribers.on_application_cancelled` turns into
    `cancel_invoice_for_application`. `applications.service.cancel` (branch
    2's own flow verb) does not exist on this branch yet — publishing the
    event directly is exactly the payload contract `subscribers.py`'s own
    docstring commits to (`application_id` only), so this fixture exercises
    the SAME subscriber wiring branch 2's real `cancel()` will eventually
    drive, not a shortcut around it."""
    await publish(
        db,
        Event(
            name=APPLICATION_CANCELLED, payload={"application_id": pending_invoice.application_id}
        ),
    )
    await db.commit()
    await db.refresh(pending_invoice)
    assert pending_invoice.status == "cancelled"
    return pending_invoice


@pytest.fixture
async def expired_invoice(db: AsyncSession, approved_application: Application) -> Invoice:
    """A `pending` invoice whose `due_at` is already in the past — task 5's
    `ERR-PAY-002` refusal checks the WALL CLOCK against `due_at` directly,
    never `status`: Task 6's expiry job (not on this branch) is what
    eventually flips `status` to `'expired'`, so a row can be past its own
    window while still reading `status='pending'` — exactly this shape,
    built directly (not through `pending_invoice`'s event path) since "past
    due" is a plain column value, not a distinct business transition of its
    own."""
    now = datetime.now(UTC)
    row = Invoice(
        application_id=approved_application.id,
        number=f"INV-2027-{uuid.uuid4().hex[:6]}",
        amount=Decimal("100.00"),
        status="pending",
        issued_at=now - timedelta(days=20),
        due_at=now - timedelta(days=10),
    )
    db.add(row)
    await db.flush()
    return row


# --- HTTP clients (Task 2 — payments's first router.py; Task 4 adds `client`) -


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    """The same guard every other HTTP-tested module's conftest carries
    (lesson: "A module's test conftest needs plumbing copied from an existing
    one, not just fixtures") — `payments` had none before Task 2, since
    Task 1 drove no HTTP requests at all. Task 4 additionally sets
    `PAYME_CASHBOX_KEY` so `test_payme_rpc.py`'s hardcoded `test-cashbox-key`
    authenticates against something real (ruling K: 'tests need a known key;
    set it the way the other modules' tests set their own mode/secret
    settings' — mirrors `test_eskiz_callback.py`'s `ESKIZ_CALLBACK_SECRET`).
    Task 5 additionally sets `PAYME_MERCHANT_ID` — `build_checkout_url`
    reads it for real in both `payme_mode` values, so `test_intents.py`'s
    determinism has to come from somewhere."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    monkeypatch.setenv("PAYME_CASHBOX_KEY", PAYME_TEST_CASHBOX_KEY)
    monkeypatch.setenv("PAYME_MERCHANT_ID", PAYME_TEST_MERCHANT_ID)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _budget_recipient_is_routable(engine) -> None:
    """Stage 7.9 task 6 (decision #160): the seeded `budget_50` row
    (migration `0045`) carries no `payme_account_id` — "to be filled in by
    the Agency" is its own seed comment, decision #159 still open — and
    `payme.py`'s new `CheckPerformTransaction`/`CreateTransaction` refusal
    (`-31008`) treats that as "this split cannot be routed" for EVERY
    invoice built while it is active, which migration `0045` makes true of
    a fresh test database by default. Every test in this whole package
    predates that routability concept except `test_payme_receivers.py`
    (which builds its OWN unrouted fixtures on purpose, deliberately
    independent of this default), so this autouse fixture gives the seeded
    row a fixed TEST-ONLY Payme id unconditionally — the same posture
    `_app_on_test_db` above already takes for the cashbox key.

    Through `engine`/`make_session_factory`, never `db` (whose own
    transaction rolls back at teardown) — the SAME idiom `budget_50_inactive`
    below already uses for mutating this identical seeded row, needed
    because the app's own HTTP requests (the `client` fixture) run on a
    SEPARATE connection that only ever sees COMMITTED rows. Not restored
    afterward (unlike `budget_50_inactive`'s own `active` toggle): nothing
    in this package asserts `payme_account_id is None` on the seeded row
    except `test_recipients_api.py`'s own `_reset_payment_recipients`,
    which unconditionally rewrites every column of interest — including
    this one — on its OWN teardown regardless of what state it inherits."""
    factory = make_session_factory(engine)
    async with factory() as session:
        await session.execute(
            update(PaymentRecipient)
            .where(PaymentRecipient.id == BUDGET_RECIPIENT_ID)
            .values(payme_account_id=PAYME_TEST_BUDGET_ACCOUNT_ID)
        )
        await session.commit()


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """An ANONYMOUS client for the Payme JSON-RPC endpoint — Payme
    authenticates via `Authorization: Basic ...` on each request body, never
    a cookie session (mirrors `notifications/test_eskiz_callback.py`'s own
    client for the identical shape of route). `_commit_pending_before_requests`
    commits whatever `pending_invoice`/`cancelled_invoice`/etc. staged on
    `db` before every outgoing call, regardless of fixture parameter order
    (same lesson's second half: pytest instantiates fixtures left-to-right,
    so a client's own setup-time commit only covers the ones listed before
    it)."""
    async with make_client(create_app(), lifespan=True) as http_client:
        _commit_pending_before_requests(http_client, db)
        yield http_client


@dataclass
class FrozenClock:
    current: datetime

    def advance(self, delta: timedelta) -> None:
        self.current += delta


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> FrozenClock:
    """Ruling G: `payme_router.py` resolves `now` through its own
    module-level `_now()` helper, and this fixture patches THAT name
    directly — never `app.core.time`, which that route does not use at all.
    `.advance(...)` moves the clock forward with no real wall-clock time
    passing, which is what lets
    `test_a_transaction_older_than_twelve_hours_is_cancelled_with_reason_four`
    prove the 12h timeout without an actual 12-hour test run."""
    clock = FrozenClock(current=datetime.now(UTC))
    monkeypatch.setattr(payme_router, "_now", lambda: clock.current)
    return clock


@pytest.fixture
async def owner_client(db: AsyncSession, applicant: Applicant) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in client for the SAME user `applicant`/`approved_application`
    belong to — the 'owner sees it' authorization case. `applicant_client`
    (gis/conftest.py) cannot stand in here: it builds its OWN unrelated
    applicant, not this fixture's."""
    assert applicant.owner_user_id is not None  # the `applicant` fixture always sets it
    user = await db.get(User, applicant.owner_user_id)
    assert user is not None
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client


@pytest.fixture
async def payments_view_client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """THE `accountant`, not an actor shaped like one: the production role
    migration 0017 grants `payments.view` and `payments.manage` to, signed in
    with no personal grants of its own.

    It was `_client_for(db, PAYMENTS_VIEW)` until 2026-09-03 — which builds an
    `executor_staff` user and bolts the code on as a `user_permissions` row.
    That proves the permission CODE works and says nothing about whether the
    role delivers it: revoke the grant in 0017 and the fixture sails through,
    while every real accountant gets a 403. The same shape
    `tests/modules/permits/conftest.py::_signer_for` uses, and for the same
    reason (lesson: a fixture's permission list must mirror the PRODUCTION
    role's grants — a fixture that lists them inherits nothing).

    Zone-free (`organization_id` unset), so an invoice read is not also a zone
    test.
    """
    user = await make_user(db, role_code="accountant")
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client


# `applicant_client` is imported above rather than rewritten (same reasoning as
# tests/modules/norms/conftest.py's own note): a fully registered applicant,
# unrelated to `approved_application`'s own applicant — the 'a stranger gets
# 404' case.


# --- Task 3: the recipients directory's own actors ---------------------------


def _session_headers(token: str, csrf: str) -> dict[str, str]:
    """A signed-in actor as a plain header dict — `session`/`csrf_token` are
    normally COOKIES (`auth_client` above sets them that way), but
    `get_current_session` only ever reads `request.cookies.get("session")`
    and `request.headers.get("X-CSRF-Token")`, and Starlette parses an
    incoming `Cookie` header into `request.cookies` exactly the same as a
    real cookie jar would. `test_recipients_api.py`'s tests pass this dict as
    `headers=` on each call rather than attaching it to `client` itself, so
    one test can compare two different actors against the same route."""
    return {"Cookie": f"session={token}", "X-CSRF-Token": csrf}


@pytest.fixture
async def sys_admin(db: AsyncSession) -> dict[str, str]:
    """Headers for a signed-in superuser. Per that task's Override 2,
    `PAYMENTS_RECIPIENTS_MANAGE` is granted to no role — `require_permission`
    lets `sys_admin` through before it ever checks a code (decision #41
    ruling 2), so this is, in practice, the only actor that can write the
    directory today."""
    user = await make_user(db, role_code="sys_admin")
    _, token, csrf = await make_session(db, user)
    return _session_headers(token, csrf)


@pytest.fixture
async def accountant(db: AsyncSession) -> dict[str, str]:
    """Headers for THE production `accountant` role (migration 0017's own
    grant of `payments.view`), not a role bolted with a personal permission
    row — same reasoning as `payments_view_client` above, restated here
    because this fixture returns headers instead of a whole client."""
    user = await make_user(db, role_code="accountant")
    _, token, csrf = await make_session(db, user)
    return _session_headers(token, csrf)


@pytest.fixture
async def budget_50(db: AsyncSession) -> PaymentRecipient:
    """The budget row migration `0045` seeds (id `BUDGET_RECIPIENT_ID`,
    50% active) — READ, never inserted a second time (that task's own
    Override 3: `payment_recipients` is not empty in a fresh test database).
    The migration module's name starts with a digit and cannot be imported
    (`import 0045_payment_split` is not valid Python), so the id is
    duplicated here as a literal — the same idiom
    `tests/modules/permits/conftest.py::APIARY_LAYOUT_FILE_ID` already uses
    for an identical reason."""
    row = await db.get(PaymentRecipient, BUDGET_RECIPIENT_ID)
    assert row is not None, "migration 0045 did not seed the budget recipient"
    return row


@pytest.fixture
async def budget_50_inactive(
    engine, budget_50: PaymentRecipient
) -> AsyncIterator[PaymentRecipient]:
    """`budget_50`, deactivated for the duration of ONE test, then restored.

    `budget_50` is not a fixture-created row — it is THE ONE seeded budget
    recipient, shared and persistent across every test in this worker's run
    (lesson: "The test DB is shared, persistent, and never empty — including
    the spot you picked"). A bare `active=False` write through `db` would
    leave it deactivated for every test that runs afterwards on this worker,
    including ones that assume an active 50% budget row. Mirrors
    `tests/modules/gis/conftest.py::_restore_gis_layers` and
    `tests/modules/permits/conftest.py::override_required_signatures`: the
    mutation and its restore both go through the `engine`'s OWN session,
    never `db` — `db`'s teardown `rollback()` cannot undo a write the app's
    separate connection already committed, and committing the restore on
    `db` would also commit whatever the test body itself left pending on it."""
    factory = make_session_factory(engine)
    async with factory() as session:
        await session.execute(
            update(PaymentRecipient).where(PaymentRecipient.id == budget_50.id).values(active=False)
        )
        await session.commit()
    yield budget_50
    async with factory() as session:
        await session.execute(
            update(PaymentRecipient).where(PaymentRecipient.id == budget_50.id).values(active=True)
        )
        await session.commit()
