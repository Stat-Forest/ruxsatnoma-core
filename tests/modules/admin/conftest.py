"""admin API tests drive a real app (create_app + lifespan) against the test DB,
and need a signed-in user: the /refs endpoints are authenticated (ruling 10).

`leshoz` is reused rather than reimplemented (`tests/modules/gis/conftest.py`
exports it as a plain importable, the same idiom `tests/modules/applications/
conftest.py` and `tests/modules/oversight/conftest.py` already use)."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, User
from tests.modules.auth.test_sessions import make_user
from tests.modules.gis.conftest import leshoz as leshoz  # noqa: F401


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def agency(db) -> Organization:
    """The one root organization every hierarchy test hangs off.

    Ruling 6 allows exactly one `agency` row, and API tests commit theirs, so this is
    a committed get-or-create shared by the whole admin suite — never a fresh row per
    test (the partial unique index would reject the second one).
    """
    existing = (
        await db.execute(select(Organization).where(Organization.kind == "agency"))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    org = Organization(
        kind="agency",
        code="agency",
        name={"uz_cyrl": "Ўрмон хўжалиги агентлиги", "en": "Forestry Agency"},
    )
    db.add(org)
    await db.commit()
    return org


# --- Stage 7.6: the open-work guard (findings F4/F5) -------------------------


def unique_pinfl() -> str:
    """Leading digit 1 — the same convention `tests/modules/applications/
    conftest.py::unique_pinfl` uses; safe to repeat here, the space is 10**13
    wide and a collision is a matter of luck, not of which suite claims which
    digit."""
    return f"1{uuid.uuid4().int % 10**13:013d}"


@pytest.fixture
async def sys_admin(db: AsyncSession) -> User:
    """The superuser `delete_user`/`archive_organization` are exercised as in
    this file's tests, which call the SERVICE directly (no route, no
    permission check to satisfy) — kept as `sys_admin` anyway so a reader
    sees the same actor a real caller would use."""
    return await make_user(db, role_code="sys_admin", pinfl=unique_pinfl())


@pytest.fixture
async def staff_user(db: AsyncSession) -> User:
    """A plain staff account holding nothing — `delete_user`'s happy path."""
    return await make_user(db, role_code="executor_staff")


@pytest.fixture
async def reviewer(db: AsyncSession) -> User:
    """A second staff account, distinct from `staff_user`, for a test that
    assigns an application to it and then tries to delete it."""
    return await make_user(db, role_code="executor_staff")


async def make_bare_application(
    db: AsyncSession,
    *,
    status: str = "SUBMITTED",
    assigned_user_id: uuid.UUID | None = None,
    assigned_org_id: uuid.UUID | None = None,
) -> Application:
    """An `applications` row built directly through the ORM rather than the
    real submission flow (mirrors `tests/modules/oversight/
    conftest.py::make_bare_application`'s identical reasoning): this suite's
    only interest is the OPEN-WORK GUARD — a read of `assigned_user_id` /
    `assigned_org_id` and `status` — never the application lifecycle those
    columns are normally reached through.

    `contour_id`/`period_from`/`period_to` all stay null, which keeps
    `ex_applications_no_duplicate` out of scope for every status this helper
    is asked to build (the EXCLUDE constraint's WHERE requires all three to
    be set), so `status="CLOSED"` is exactly as constructible as
    `status="SUBMITTED"`.
    """
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
        channel="portal",
        status=status,
        assigned_user_id=assigned_user_id,
        assigned_org_id=assigned_org_id,
    )
    db.add(application)
    await db.flush()
    return application
