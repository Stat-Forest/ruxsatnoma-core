"""Fixtures for the permits module.

Rows are built directly through the ORM, never through an API: `applications`
has no `router.py` on `dev` (3.9a shipped models/service/repo/events only), so
there is no route that creates an application to drive. The spatial primitives
come from `tests/modules/gis/conftest.py` as plain importables — the same idiom
`tests/modules/applications/conftest.py` and `tests/modules/norms/conftest.py`
already use; two ways to build a contour is how the two drift apart.

Each application gets its OWN applicant and its OWN contour. That is not
tidiness: `ex_applications_no_duplicate` (migration 0015) forbids two
applications for the same (applicant, contour, activity) on an overlapping
period whenever the status is one of the active ones — and `PAID` is one of
them — so two `PAID` fixtures sharing an applicant and a contour would fail at
insert, before the permit test they exist for ever ran.

No `_app_on_test_db` fixture: this branch drives no HTTP requests, only
direct-ORM tests against the `db` fixture (a later task adding the module's
first `create_app()` test must add it here — lesson).
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import MediaFile
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant
from app.modules.gis.models import GisLayer
from tests.modules.auth.test_sessions import make_user
from tests.modules.gis.conftest import approval_doc as approval_doc
from tests.modules.gis.conftest import contours_layer as contours_layer
from tests.modules.gis.conftest import leshoz as leshoz
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt


def unique_pinfl() -> str:
    """Leading digit 1 is what `tests/modules/applications/conftest.py` uses; the
    remaining 13 digits are random, so two modules sharing this persistent test
    DB never collide on `uq_users_pinfl`."""
    return f"1{uuid.uuid4().int % 10**13:013d}"


@pytest.fixture
async def grazing_activity_id(db: AsyncSession) -> uuid.UUID:
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'grazing'"))
    return rows.scalar_one()


async def make_paid_application(
    db: AsyncSession,
    *,
    layer: GisLayer,
    org: Organization,
    approval_doc: MediaFile,
    activity_type_id: uuid.UUID,
) -> Application:
    """An application in `PAID` — the only status this module issues a permit
    from (ruling 10) — with its own applicant, contour and published version, at
    a random, isolated spot (a fixed committed geometry accumulates across runs
    on the shared test DB — lesson)."""
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

    row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=user.id,
        on_behalf="self",
        activity_type_id=activity_type_id,
        contour_id=contour.id,
        contour_version_id=version.id,
        requested_area_ha=Decimal("12.5000"),
        period_from=date(2027, 5, 1),
        period_to=date(2027, 9, 30),
        status="PAID",
        channel="portal",
        assigned_org_id=org.id,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def paid_application(
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    grazing_activity_id: uuid.UUID,
) -> Application:
    return await make_paid_application(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=grazing_activity_id,
    )


@pytest.fixture
async def second_paid_application(
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    grazing_activity_id: uuid.UUID,
) -> Application:
    """A second, fully independent application — the one a uniqueness test needs
    so the row it inserts collides on `(series, number)` and not on
    `permits.application_id`, which is unique too."""
    return await make_paid_application(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=grazing_activity_id,
    )
