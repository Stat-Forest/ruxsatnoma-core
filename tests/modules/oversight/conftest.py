"""Fixtures for `oversight` (and, imported from here, `dashboard`). Rows are
built directly through the ORM — the same idiom `tests/modules/permits/
conftest.py` uses for `make_permit_on_contour`, reused here rather than
copied (`make_bare_application` mirrors its inline `Application` construction
for a case with no contour at all)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant
from tests.modules.auth.test_sessions import make_user
from tests.modules.gis.conftest import _client_for as _client_for  # noqa: F401
from tests.modules.gis.conftest import (
    _commit_pending_before_requests as _commit_pending_before_requests,  # noqa: F401
)
from tests.modules.gis.conftest import leshoz as leshoz  # noqa: F401
from tests.modules.gis.conftest import other_leshoz as other_leshoz  # noqa: F401
from tests.modules.permits.conftest import HOLDER_NAME, unique_pinfl


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    """The same guard every HTTP-tested package carries (lesson): the app must
    open the TEST database, not the shared dev one."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def make_bare_application(
    db: AsyncSession, *, org: Organization, contour_id: uuid.UUID | None = None
) -> Application:
    """An `applications` row with just enough set to exist and carry a zone —
    `assigned_org_id=org.id` directly, so no contour is needed at all for a
    test whose only interest is zone RESOLUTION, not the application
    lifecycle (mirrors `make_permit_on_contour`'s own inline construction,
    narrowed to the one thing this fixture needs)."""
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    user.full_name = HOLDER_NAME
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
        assigned_org_id=org.id,
        contour_id=contour_id,
        submitted_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    db.add(application)
    await db.flush()
    return application
