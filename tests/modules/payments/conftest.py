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
'A module's first HTTP-driven test file needs its own `_app_on_test_db` guard')."""

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.main import create_app
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, User
from app.modules.norms.models import Calculation
from app.modules.payments.models import Invoice
from app.modules.payments.permissions import PAYMENTS_VIEW
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.applications.conftest import applicant as applicant
from tests.modules.applications.conftest import grazing_activity_id as grazing_activity_id
from tests.modules.auth.test_sessions import make_session
from tests.modules.gis.conftest import _client_for, _commit_pending_before_requests
from tests.modules.gis.conftest import applicant_client as applicant_client


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


# --- HTTP clients (Task 2 — payments's first router.py) ---------------------


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    """The same guard every other HTTP-tested module's conftest carries
    (lesson: 'A module's first HTTP-driven test file needs its own
    `_app_on_test_db` guard') — `payments` had none before Task 2, since
    Task 1 drove no HTTP requests at all."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
