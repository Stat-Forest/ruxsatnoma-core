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

Task 3 adds the module's first HTTP-driven tests, so `_app_on_test_db` lands
here too (lesson: without it `create_app()` opens the shared DEV database and
every request 401s with no hint that the database is the bug).
"""

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import storage
from app.core.models import MediaFile
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, User
from app.modules.gis.models import GisLayer
from app.modules.norms.calculator import RULE_CODE_VERSION
from app.modules.norms.models import Calculation
from app.modules.permits.models import PermitTemplate
from app.modules.permits.permissions import PERMITS_ISSUE
from tests.modules.auth.test_sessions import make_user
from tests.modules.gis.conftest import (
    _client_for,
    applicant_client,  # noqa: F401 — a fixture imported into a conftest IS available
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
    """The same guard `tests/modules/gis/conftest.py` and
    `tests/modules/norms/conftest.py` carry: the app under test must open the
    TEST database, not the dev one. An autouse fixture applies only inside its
    own package, so importing gis's helpers does NOT bring it along."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def unique_pinfl() -> str:
    """Leading digit 1 is what `tests/modules/applications/conftest.py` uses; the
    remaining 13 digits are random, so two modules sharing this persistent test
    DB never collide on `uq_users_pinfl`."""
    return f"1{uuid.uuid4().int % 10**13:013d}"


@pytest.fixture
async def grazing_activity_id(db: AsyncSession) -> uuid.UUID:
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'grazing'"))
    return rows.scalar_one()


@pytest.fixture
async def haymaking_activity_id(db: AsyncSession) -> uuid.UUID:
    """A second activity type, for the `permit_templates` uniqueness tests: migration
    0019 already seeds an ACTIVE grazing template, so a test that built its own v1
    there would collide with the seed rather than with the row it created."""
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'haymaking'"))
    return rows.scalar_one()


@pytest.fixture
async def apiary_activity_id(db: AsyncSession) -> uuid.UUID:
    """A THIRD activity type, for the stored-layout arm of the template lookup.
    Deliberately not `haymaking`: `test_models.py` inserts its own ACTIVE haymaking
    template and needs the slot empty, while the fixture below has to COMMIT its
    template for the app's own session to see it."""
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'apiary'"))
    return rows.scalar_one()


async def make_paid_application(
    db: AsyncSession,
    *,
    layer: GisLayer,
    org: Organization,
    approval_doc: MediaFile,
    activity_type_id: uuid.UUID,
    status: str = "PAID",
    used_sb: Decimal | None = Decimal("40.0000"),
    with_calculation: bool = True,
) -> Application:
    """An application in `PAID` — the only status this module issues a permit
    from (ruling 10) — with its own applicant, contour and published version, at
    a random, isolated spot (a fixed committed geometry accumulates across runs
    on the shared test DB — lesson).

    It also gets a `Calculation`, because issuance reads the priced amount out of
    `applications.service.current_calculation` and refuses without one (`tz/13`
    field 18 is not optional on a permit). `used_sb=None` is the shape of an
    activity that commits no conditional-head load at all — haymaking, apiaries —
    where the permit's `sb_load` stays null (task 1, decision 3).

    `with_calculation=False` is how a test reaches the "no calculation" refusal:
    `calculations` is append-only at the database level (migration 0011's trigger),
    so a row cannot be deleted afterwards — the application has to be built
    without one."""
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
        status=status,
        channel="portal",
        assigned_org_id=org.id,
    )
    db.add(row)
    await db.flush()

    if not with_calculation:
        return row

    db.add(
        Calculation(
            application_id=row.id,
            contour_id=contour.id,
            activity_type_id=activity_type_id,
            rule_code_version=RULE_CODE_VERSION,
            input_snapshot={"source": "test fixture"},
            used_sb=used_sb,
            amount=Decimal("2060000.00"),
            breakdown={"total": "2060000.00"},
        )
    )
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


@pytest.fixture
async def approved_application(
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    grazing_activity_id: uuid.UUID,
) -> Application:
    """An application that has NOT been paid. Built directly, never through the
    API: once 3.10a is merged its invoice subscriber moves an application from
    APPROVED to INVOICED inside the approval's own transaction (3.10a ruling 14),
    so APPROVED is unreachable through any endpoint. The refusal this fixture
    exists for holds for either status — do not rewrite it to go through
    `approve` on the assumption that it can."""
    return await make_paid_application(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=grazing_activity_id,
        status="APPROVED",
    )


@pytest.fixture
async def unpriced_application(
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    grazing_activity_id: uuid.UUID,
) -> Application:
    """PAID, but with no `Calculation` — `tz/13` field 18 has no source. Built
    without one rather than stripped afterwards: `calculations` is append-only at
    the database level, so `DELETE FROM calculations` raises."""
    return await make_paid_application(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=grazing_activity_id,
        with_calculation=False,
    )


@pytest.fixture
async def apiary_paid_application(
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    apiary_activity_id: uuid.UUID,
) -> Application:
    """A paid application for the activity whose template carries a STORED layout.
    `used_sb=None` because an apiary commits no conditional-head load — which is
    also what makes the stored layout below legitimately narrower than the
    bundled one."""
    return await make_paid_application(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=apiary_activity_id,
        used_sb=None,
    )


@pytest.fixture
async def applicant_row(db: AsyncSession, paid_application: Application) -> Applicant:
    """The `Applicant` the permit's `holder_name`/`holder_pinfl` are copied from —
    looked up rather than returned by `make_paid_application`, so `test_models.py`'s
    own callers are untouched."""
    row = await db.get(Applicant, paid_application.applicant_id)
    assert row is not None
    return row


@pytest.fixture
async def applicant_user(db: AsyncSession, applicant_row: Applicant) -> User:
    """The individual applicant's own account (`applicants.owner_user_id`) — the
    recipient of the `permit.issued` notification."""
    assert applicant_row.owner_user_id is not None
    user = await db.get(User, applicant_row.owner_user_id)
    assert user is not None
    return user


@pytest.fixture
async def assigned_executor(
    db: AsyncSession, leshoz: Organization, paid_application: Application
) -> User:
    """The hodim the application is assigned to — who `payment_confirmed` tells
    that a permit is now due (ruling 19). `make_paid_application` leaves
    `assigned_user_id` null on purpose, so the "nobody assigned" arm has a fixture
    of its own: the plain `paid_application`."""
    user = await make_user(db, role_code="executor_staff", organization_id=leshoz.id)
    paid_application.assigned_user_id = user.id
    await db.flush()
    return user


# --- the stored-layout arm of the template lookup ----------------------------

STORED_LAYOUT = (
    "<html><body><h1>{{ series }} № {{ number }}</h1>"
    '<p>Асаларичилик — {{ holder_name }}</p><img src="{{ qr }}"></body></html>'
)


# A FIXED id and storage key, so the fixture is get-or-create rather than
# create-and-clean-up. It cannot clean up: the test issues a permit against this
# template, `permits.template_id` is an FK to it, and an issued permit's template
# must resolve forever — so a teardown DELETE raises ForeignKeyViolation, and the
# permit itself cannot be deleted either (`permit_status_history` is append-only
# at the database level). Idempotent setup is the only shape that survives a
# second run on this shared, persistent test DB.
APIARY_LAYOUT_FILE_ID = uuid.UUID("01a06200-0000-7000-8000-000000000001")
APIARY_LAYOUT_KEY = "t/permits-apiary-layout.html"


@pytest.fixture
async def apiary_template(
    db: AsyncSession, apiary_activity_id: uuid.UUID
) -> AsyncIterator[PermitTemplate]:
    """An ACTIVE template whose `layout_file_id` points at a stored HTML file —
    the arm migration 0019's seeded grazing row (null `layout_file_id`, meaning
    "the layout bundled in `assets/`") does not exercise.

    Committed, because the app runs on its own session and connection. Reused
    rather than recreated when a previous run left it behind — the same
    get-or-create shape `tests/modules/gis/conftest.py::leshoz` uses for the
    singleton agency row, and here it is not a nicety but the only option (see
    the constants above). The object is re-uploaded every time, so the stored
    bytes can never drift from `STORED_LAYOUT` the test compares against.
    """
    await storage.ensure_bucket()
    data = STORED_LAYOUT.encode("utf-8")
    await storage.put_object(APIARY_LAYOUT_KEY, data, "text/html")

    file = await db.get(MediaFile, APIARY_LAYOUT_FILE_ID)
    if file is None:
        file = MediaFile(
            id=APIARY_LAYOUT_FILE_ID,
            storage_key=APIARY_LAYOUT_KEY,
            filename="apiary_layout.html",
            content_type="text/html",
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        db.add(file)
        await db.flush()

    template = (
        await db.execute(
            select(PermitTemplate).where(
                PermitTemplate.activity_type_id == apiary_activity_id,
                PermitTemplate.status == "active",
            )
        )
    ).scalar_one_or_none()
    if template is None:
        template = PermitTemplate(
            activity_type_id=apiary_activity_id,
            version=1,
            name={"uz_cyrl": "Асаларичилик шакли", "ru": "Форма для пасек"},
            layout_file_id=file.id,
            status="active",
            valid_from=date(2027, 1, 1),
        )
        db.add(template)
    # Self-healing rather than asserting: this row is the fixture's OWN, left
    # behind by a previous run, and an earlier version of this fixture pointed it
    # at a randomly-named file. Repointing it keeps the one active apiary template
    # consistent with the constants above, which is what the test compares against.
    template.layout_file_id = file.id
    await db.commit()
    yield template


# --- clients -----------------------------------------------------------------
# `_client_for(db, *permissions, organization_id=None)` is an async generator
# (gis/conftest.py): drive it with `async for`. Permissions are POSITIONAL.


@pytest.fixture
async def hodim_client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """The «ходим» who forms the document — `permits.issue`, which migration 0019
    grants to `executor_staff`, the very role `_client_for` builds. Zone-free, so
    the happy path is not also a zone test."""
    async for client in _client_for(db, PERMITS_ISSUE):
        yield client


@pytest.fixture
async def other_zone_hodim_client(
    db: AsyncSession, other_leshoz: Organization
) -> AsyncIterator[httpx.AsyncClient]:
    """A `permits.issue` holder zoned to a DIFFERENT leshoz — zone scoping is not
    a permission check (lesson), and issuance is a write path that needs both."""
    async for client in _client_for(db, PERMITS_ISSUE, organization_id=other_leshoz.id):
        yield client


# `applicant_client` (re-exported above) is what the permission-denial test uses.
# A grantless `_client_for(db)` would NOT do: it builds an `executor_staff` user,
# and migration 0019 grants `permits.issue` to that very ROLE — the actor would
# hold the code through `role_permissions` and sail past the route's dependency
# (lesson: a fixture's permission list must mirror the production role's grants).
