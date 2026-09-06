"""Fixtures for the reports module.

Most of the lifecycle is exercised directly against `service.py` with plain
`User` rows (`make_user`) rather than through HTTP: the identity checks this
module cares about (`_assert_report_signer`) read the actor's ROLE, and a
service-level `User(role_code="executor_head", ...)` proves that directly and
far more cheaply than a signed-in HTTP client. HTTP coverage
(`test_router.py`) is reserved for permission-gate 403s and one full
round-trip through the real routes.

`make_permit_on_contour` and `HOLDER_NAME` are imported from
`tests.modules.permits.conftest` (which itself imports gis fixtures) rather
than rebuilt here — a permit fixture already exists with exactly the
FK chain (`applicants` -> `applications` -> `permits`) this module's own
reader join (`repo.report_rows`) needs, and reports has no writer of its own
that could produce one.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.auth.models import User
from app.modules.gis.models import GisLayer
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.payments.models import Invoice
from app.modules.permits.models import Permit
from app.modules.reports import forms_seed, service
from app.modules.reports.models import ReportForm
from app.modules.reports.permissions import REPORTS_MANAGE
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.gis.conftest import (
    _client_for,
    _commit_pending_before_requests,
    make_contour,
    make_version,
    random_box_wkt,
)
from tests.modules.gis.conftest import approval_doc as approval_doc
from tests.modules.gis.conftest import contours_layer as contours_layer
from tests.modules.gis.conftest import leshoz as leshoz
from tests.modules.gis.conftest import other_leshoz as other_leshoz
from tests.modules.permits.conftest import make_permit_on_contour, unique_pinfl


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    """The app under test must open the TEST database, not the dev one
    (lesson) — same guard `permits`/`gis`/`norms` each carry, package-scoped."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def grazing_activity_id(db: AsyncSession) -> uuid.UUID:
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'grazing'"))
    return rows.scalar_one()


@pytest.fixture
async def central_admin_user(db: AsyncSession) -> User:
    return await make_user(db, role_code="central_admin")


@pytest.fixture
async def grazing_form(
    db: AsyncSession, grazing_activity_id: uuid.UUID, central_admin_user: User
) -> ReportForm:
    """An ACTIVE 2-ilova form — `forms_seed.GRAZING_COLUMNS`, the real payload
    a central admin's `POST /reports/forms` would carry.

    `code` carries a random suffix: `uq_report_forms_code_version` is a real
    database constraint, and at least one caller of this fixture COMMITS (the
    happy-path test asserting the freeze trigger, which needs committed rows
    to safely test a rollback-then-continue) — a literal code would collide
    with itself on the second run against this shared, persistent test DB
    (lesson)."""
    form = await service.create_form(
        db,
        code=f"{forms_seed.GRAZING_FORM_CODE}-{uuid.uuid4().hex[:8]}",
        version=1,
        name={"uz_cyrl": "2-илова", "ru": "2-ilova"},
        activity_type_id=grazing_activity_id,
        period_type="quarter",
        columns=forms_seed.GRAZING_COLUMNS,
        rules=[],
        schedule={},
        valid_from=date(2027, 1, 1),
        actor=central_admin_user,
    )
    return await service.activate_form(db, form.id, central_admin_user)


async def make_report_permit(
    db: AsyncSession,
    *,
    layer: GisLayer,
    org: Organization,
    approval_doc,
    activity_type_id: uuid.UUID,
    period_from: date,
    period_to: date,
    paid_amount: Decimal | None = None,
) -> Permit:
    """One permit + applicant + application, through
    `tests.modules.permits.conftest.make_permit_on_contour` — the FK chain
    `repo.report_rows`' join reads. `paid_amount` additionally inserts a
    `paid` invoice, so a report's `paid_amount` column has something to show."""
    contour = await make_contour(db, layer, org)
    version = await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    permit = await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version.id,
        org=org,
        activity_type_id=activity_type_id,
        status="active",
        period_from=period_from,
        period_to=period_to,
    )
    if paid_amount is not None:
        db.add(
            Invoice(
                number=f"INV-{uuid.uuid4().hex[:10]}",
                application_id=permit.application_id,
                amount=paid_amount,
                status="paid",
            )
        )
        await db.flush()
    return permit


# --- clients (HTTP, for test_router.py) -------------------------------------


@pytest.fixture
async def hodim_client(db: AsyncSession, leshoz: Organization) -> AsyncIterator[httpx.AsyncClient]:
    """`reports.manage` scoped to `leshoz` — the hodim who fills a report."""
    async for client in _client_for(db, REPORTS_MANAGE, organization_id=leshoz.id):
        yield client


@pytest.fixture
async def viewer_client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """Holds no `reports.*` permission at all — the 403 case.

    Deliberately NOT `_client_for(db)` with no codes: that builds an
    `executor_staff` user, and migration 0027 grants THAT ROLE both
    `reports.view` and `reports.manage` — a "grantless" fixture would
    silently inherit them and sail past the gate (lesson: a fixture's
    permission list must mirror the production role's grants). `inspector`
    is the one role tz/03's matrix marks "—" for every reports column.
    """
    user = await make_user(db, role_code="inspector")
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client


@dataclass(frozen=True)
class Signer:
    """A signed-in client under a REAL role — `permits/conftest.py`'s exact
    shape, needed because `_client_for` always builds `executor_staff` with
    personal grants, which proves nothing about a check that reads the
    actor's ROLE (`service._assert_report_signer`)."""

    client: httpx.AsyncClient
    user: User
    pinfl: str


async def _signer_for(
    db: AsyncSession, *, role_code: str, organization_id: uuid.UUID | None = None
) -> AsyncIterator[Signer]:
    user = await make_user(
        db, role_code=role_code, organization_id=organization_id, pinfl=unique_pinfl()
    )
    _, token, csrf = await make_session(db, user)
    await db.commit()
    assert user.pinfl is not None
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield Signer(client=client, user=user, pinfl=user.pinfl)


@pytest.fixture
async def head_signer(db: AsyncSession, leshoz: Organization) -> AsyncIterator[Signer]:
    async for signer in _signer_for(db, role_code="executor_head", organization_id=leshoz.id):
        yield signer


@pytest.fixture
async def central_admin_signer(db: AsyncSession) -> AsyncIterator[Signer]:
    async for signer in _signer_for(db, role_code="central_admin"):
        yield signer


async def sign_report_request(
    signer: Signer, report_id: uuid.UUID, document: bytes
) -> httpx.Response:
    return await signer.client.post(
        f"/api/v1/reports/{report_id}/sign",
        json={
            "pkcs7": encode_mock_signature(
                document=document, serial=f"SER-{signer.pinfl}", issuer="ISS-1", pinfl=signer.pinfl
            )
        },
    )
