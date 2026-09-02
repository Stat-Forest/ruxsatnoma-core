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
'A module's first HTTP-driven test file needs its own `_app_on_test_db` guard').

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
authenticates against something real."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.events import Event, publish
from app.main import create_app
from app.modules.applications.events import APPLICATION_APPROVED, APPLICATION_CANCELLED
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, User
from app.modules.norms.models import Calculation
from app.modules.payments import payme_router
from app.modules.payments import service as payments_service
from app.modules.payments.models import Invoice
from app.modules.payments.permissions import PAYMENTS_VIEW
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.applications.conftest import applicant as applicant
from tests.modules.applications.conftest import grazing_activity_id as grazing_activity_id
from tests.modules.auth.test_sessions import make_session
from tests.modules.gis.conftest import _client_for, _commit_pending_before_requests
from tests.modules.gis.conftest import applicant_client as applicant_client

PAYME_TEST_CASHBOX_KEY = "test-cashbox-key"


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
async def pending_invoice(db: AsyncSession, approved_application: Application) -> Invoice:
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
    this fixture — not the implementation — was wrong."""
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


# --- HTTP clients (Task 2 — payments's first router.py; Task 4 adds `client`) -


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    """The same guard every other HTTP-tested module's conftest carries
    (lesson: 'A module's first HTTP-driven test file needs its own
    `_app_on_test_db` guard') — `payments` had none before Task 2, since
    Task 1 drove no HTTP requests at all. Task 4 additionally sets
    `PAYME_CASHBOX_KEY` so `test_payme_rpc.py`'s hardcoded `test-cashbox-key`
    authenticates against something real (ruling K: 'tests need a known key;
    set it the way the other modules' tests set their own mode/secret
    settings' — mirrors `test_eskiz_callback.py`'s `ESKIZ_CALLBACK_SECRET`)."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    monkeypatch.setenv("PAYME_CASHBOX_KEY", PAYME_TEST_CASHBOX_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """An ANONYMOUS client for the Payme JSON-RPC endpoint — Payme
    authenticates via `Authorization: Basic ...` on each request body, never
    a cookie session (mirrors `notifications/test_eskiz_callback.py`'s own
    client for the identical shape of route). `_commit_pending_before_requests`
    commits whatever `pending_invoice`/`cancelled_invoice`/etc. staged on
    `db` before every outgoing call, regardless of fixture parameter order
    (lesson: "A `_client_for` client's setup-time commit only covers
    fixtures listed before it")."""
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
    """An accountant-shaped actor: holds `payments.view`, zone-free — sees any
    invoice regardless of who applied for it."""
    async for client in _client_for(db, PAYMENTS_VIEW):
        yield client


# `applicant_client` is imported above rather than rewritten (same reasoning as
# tests/modules/norms/conftest.py's own note): a fully registered applicant,
# unrelated to `approved_application`'s own applicant — the 'a stranger gets
# 404' case.
