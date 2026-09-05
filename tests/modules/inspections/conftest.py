"""Fixtures for the inspections module. Spatial/organization primitives come
from `tests/modules/gis/conftest.py` as plain importables — the established
idiom `tests/modules/permits/conftest.py`/`tests/modules/applications/
conftest.py` already use.

Every staff client below uses a REAL role code (`inspector`, `executor_head`,
`executor_staff`, `central_admin`) with NO personal grant added — the
permission comes from migration `0026`'s own `ROLE_GRANTS` alone, so every
test built on these fixtures doubles as a regression check that those grants
actually work end to end through `require_permission`.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.models import MediaFile
from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, User
from app.modules.gis.models import GisLayer
from app.modules.permits.models import Permit
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.gis.conftest import (
    _commit_pending_before_requests,
    make_contour,
    make_version,
    random_box_wkt,
)
from tests.modules.gis.conftest import approval_doc as approval_doc
from tests.modules.gis.conftest import contours_layer as contours_layer
from tests.modules.gis.conftest import leshoz as leshoz
from tests.modules.gis.conftest import other_leshoz as other_leshoz


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    """The app under test must open the TEST database, not the dev one
    (lesson) — an autouse fixture applies only inside its own package, so
    importing gis's helpers does NOT bring gis's own copy along."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def unique_pinfl() -> str:
    """Leading digit 1, matching `tests/modules/applications/conftest.py`'s own
    idiom — the remaining 13 digits are random so two test modules sharing
    this persistent test DB never collide on `uq_users_pinfl`."""
    return f"1{uuid.uuid4().int % 10**13:013d}"


@pytest.fixture
async def grazing_activity_id(db: AsyncSession) -> uuid.UUID:
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'grazing'"))
    return rows.scalar_one()


@pytest.fixture
async def default_checklist_id(db: AsyncSession) -> uuid.UUID:
    """Migration `0026`'s own seeded `field_inspection_default` — its two
    REQUIRED questions are `activity_matches`/`within_contour`."""
    rows = await db.execute(
        text("SELECT id FROM checklists WHERE code = 'field_inspection_default'")
    )
    return rows.scalar_one()


async def violation_type_id(db: AsyncSession, code: str) -> uuid.UUID:
    """One of migration `0026`'s own seeded VT-01…06 items, by code."""
    rows = await db.execute(
        text("SELECT id FROM classifier_items WHERE code = :code"), {"code": code}
    )
    return rows.scalar_one()


@pytest.fixture
async def vt_01(db: AsyncSession) -> uuid.UUID:
    return await violation_type_id(db, "VT-01")


async def make_application(
    db: AsyncSession,
    *,
    layer: GisLayer,
    org: Organization,
    approval_doc: MediaFile,
    activity_type_id: uuid.UUID,
    status: str = "SUBMITTED",
) -> Application:
    """An application with its own applicant, contour and PUBLISHED version, at
    a random, isolated spot (a fixed committed geometry accumulates across
    runs on the shared test DB — lesson). Inserted directly through the ORM —
    inspections needs no pricing, no calculation, nothing `applications`'
    own submission flow produces beyond identity and geometry."""
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    applicant = Applicant(
        kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id
    )
    db.add(applicant)
    await db.flush()

    contour = await make_contour(db, layer, org)
    version = await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )

    application = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=user.id,
        on_behalf="self",
        activity_type_id=activity_type_id,
        contour_id=contour.id,
        contour_version_id=version.id,
        requested_area_ha=Decimal("5.0000"),
        period_from=date(2027, 5, 1),
        period_to=date(2027, 9, 30),
        status=status,
        channel="portal",
        assigned_org_id=org.id,
    )
    db.add(application)
    await db.flush()
    return application


@pytest.fixture
async def application(
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    grazing_activity_id: uuid.UUID,
) -> Application:
    return await make_application(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=grazing_activity_id,
    )


@pytest.fixture
async def applicant_row(db: AsyncSession, application: Application) -> Applicant:
    row = await db.get(Applicant, application.applicant_id)
    assert row is not None
    return row


@pytest.fixture
async def applicant_user(db: AsyncSession, applicant_row: Applicant) -> User:
    assert applicant_row.owner_user_id is not None
    user = await db.get(User, applicant_row.owner_user_id)
    assert user is not None
    return user


async def make_permit(
    db: AsyncSession, *, application: Application, org: Organization, status: str = "active"
) -> Permit:
    """A minimal, directly-inserted permit — inspections reads identity/status/
    zone only, never signatures or the rendered document, so the full issuance
    pipeline `tests/modules/permits/conftest.py::active_permit` drives is out
    of proportion here (the same "insert directly" convention this codebase's
    own `make_paid_application` uses for an APPLICATION at the status this
    module's tests actually need)."""
    permit = Permit(
        series="Т",
        number=uuid.uuid4().int % 2_000_000_000 + 1,
        application_id=application.id,
        applicant_id=application.applicant_id,
        activity_type_id=application.activity_type_id,
        organization_id=org.id,
        contour_id=application.contour_id,
        contour_version_id=application.contour_version_id,
        area_ha=application.requested_area_ha,
        period_from=application.period_from,
        period_to=application.period_to,
        amount=Decimal("1000000.00"),
        status=status,
        qr_token=uuid.uuid4().hex,
        snapshot={},
    )
    db.add(permit)
    await db.flush()
    return permit


@pytest.fixture
async def permit(db: AsyncSession, application: Application, leshoz: Organization) -> Permit:
    return await make_permit(db, application=application, org=leshoz)


@asynccontextmanager
async def _client_for_user(db: AsyncSession, user: User) -> AsyncIterator[httpx.AsyncClient]:
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client


@pytest.fixture
async def inspector(db: AsyncSession, leshoz: Organization) -> User:
    """`pinfl` set (14 digits): `signatures.service.sign()`'s ownership check
    needs it to match the mock certificate's own `pinfl_or_stir` before an
    act-signing test can succeed."""
    return await make_user(
        db, role_code="inspector", organization_id=leshoz.id, pinfl=unique_pinfl()
    )


@pytest.fixture
async def inspector_client(db: AsyncSession, inspector: User) -> AsyncIterator[httpx.AsyncClient]:
    async with _client_for_user(db, inspector) as client:
        yield client


@pytest.fixture
async def other_inspector(db: AsyncSession, other_leshoz: Organization) -> User:
    """A SECOND inspector, zoned to a DIFFERENT leshoz — the negative arm of
    every zone-scoping test."""
    return await make_user(
        db, role_code="inspector", organization_id=other_leshoz.id, pinfl=unique_pinfl()
    )


@pytest.fixture
async def other_inspector_client(
    db: AsyncSession, other_inspector: User
) -> AsyncIterator[httpx.AsyncClient]:
    async with _client_for_user(db, other_inspector) as client:
        yield client


@pytest.fixture
async def executor_head(db: AsyncSession, leshoz: Organization) -> User:
    return await make_user(db, role_code="executor_head", organization_id=leshoz.id)


@pytest.fixture
async def executor_head_client(
    db: AsyncSession, executor_head: User
) -> AsyncIterator[httpx.AsyncClient]:
    async with _client_for_user(db, executor_head) as client:
        yield client


@pytest.fixture
async def executor_staff(db: AsyncSession, leshoz: Organization) -> User:
    return await make_user(db, role_code="executor_staff", organization_id=leshoz.id)


@pytest.fixture
async def executor_staff_client(
    db: AsyncSession, executor_staff: User
) -> AsyncIterator[httpx.AsyncClient]:
    async with _client_for_user(db, executor_staff) as client:
        yield client


@pytest.fixture
async def central_admin(db: AsyncSession) -> User:
    """Republic-wide (no organization) — the same zone-free shape a real
    central-apparatus account has."""
    return await make_user(db, role_code="central_admin")


@pytest.fixture
async def central_admin_client(
    db: AsyncSession, central_admin: User
) -> AsyncIterator[httpx.AsyncClient]:
    async with _client_for_user(db, central_admin) as client:
        yield client


@pytest.fixture
async def applicant_client(
    db: AsyncSession, applicant_user: User
) -> AsyncIterator[httpx.AsyncClient]:
    """The individual applicant who owns `application` — the violator's own
    account for the case-explanation/appeal routes."""
    async with _client_for_user(db, applicant_user) as client:
        yield client
