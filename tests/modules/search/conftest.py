"""Fixtures for `search`. Reuses the established cross-module test idiom
(`tests/modules/permits/conftest.py` already does the same, for the same
reason): organizations and the zone-scoped client builder come from
`tests.modules.gis.conftest`, a ready-made application/permit come from
`tests.modules.applications.conftest`/`tests.modules.permits.conftest`,
never reimplemented here — two ways to build a contour (or a client) is how
the two drift apart.
"""

import uuid
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import uuid7
from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.gis.conftest import _client_for as _client_for
from tests.modules.gis.conftest import _commit_pending_before_requests
from tests.modules.gis.conftest import approval_doc as approval_doc
from tests.modules.gis.conftest import contours_layer as contours_layer
from tests.modules.gis.conftest import leshoz as leshoz
from tests.modules.gis.conftest import other_leshoz as other_leshoz
from tests.modules.permits.conftest import grazing_activity_id as grazing_activity_id
from tests.modules.permits.conftest import make_permit_on_contour as make_permit_on_contour
from tests.modules.permits.conftest import unique_pinfl


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    """The app under test must open the TEST database (lesson: an autouse
    fixture applies only within its own package, so importing gis's helpers
    does not bring gis's own copy along)."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def make_application(
    db: AsyncSession,
    *,
    org: Organization,
    status: str,
    applicant_name: str,
    activity_type_id: uuid.UUID | None = None,
    number: str | None = None,
    period_from: date = date(2027, 5, 1),
    period_to: date = date(2027, 9, 30),
) -> Application:
    """An application row in a GIVEN status, built directly through the ORM
    (`contour_id=None`: search/archive read `status`/`assigned_org_id`/the
    applicant's own text fields, never geometry, and `ex_applications_no_
    duplicate`'s WHERE clause excludes every terminal status this module's
    tests use — CANCELLED, REJECTED, EXPIRED_UNPAID, CLOSED — so a null
    contour never collides with it). Mirrors `permits.conftest.
    make_permit_on_contour`'s own justification for building a status
    directly rather than through a real transition: what this module's
    queries assert is a SQL predicate over `status`/`assigned_org_id`, and
    driving five modules' worth of real transitions to reach CLOSED would
    test `applications`/`payments`/`permits` a second time, not `search`."""
    # Just an FK target for `submitted_by_user_id` — role is irrelevant, this
    # user is never authenticated as anybody in these tests.
    submitter = await make_user(db)
    applicant = Applicant(
        kind="individual", pinfl=unique_pinfl(), name=applicant_name, phone="+998901234567"
    )
    db.add(applicant)
    await db.flush()
    application = Application(
        id=uuid7(),
        number=number or f"APP-{uuid.uuid4().hex[:8]}",
        applicant_id=applicant.id,
        submitted_by_user_id=submitter.id,
        on_behalf="self",
        activity_type_id=activity_type_id,
        status=status,
        channel="portal",
        assigned_org_id=org.id,
        period_from=period_from,
        period_to=period_to,
    )
    db.add(application)
    await db.flush()
    return application


async def _client_with_role(db: AsyncSession, role_code: str):
    """A client under a SPECIFIC system role holding no personal grants —
    `_client_for` always builds `executor_staff`, which migration `0029`
    grants `search.use` to directly, so the "holds nothing" negative test
    needs a role that migration does NOT grant it to (`inspector`)."""
    user = await make_user(db, role_code=role_code)
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client
