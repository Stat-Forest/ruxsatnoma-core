"""Fixtures for the applications module.

Reuses the gis primitives rather than re-implementing contour insertion
(`tests/modules/gis/conftest.py` exports `make_contour`, `make_version`,
`random_box_wkt`, `contours_layer`, `leshoz`, `approval_doc` as plain
importables — the same idiom `tests/modules/norms/conftest.py` already uses).

`published_contour` here mirrors `tests/modules/norms/conftest.py`'s own fixture
of the same name, not gis's: it returns the `Contour` (whose `.id` is what
`applications.contour_id` points at), not the `ContourVersion` gis's fixture
returns — the two sibling test packages deliberately give this name different
shapes, each matching what their own module's FK expects.

Branch 2's Task 3 adds this package's first HTTP-driven tests, so the three
pieces of conftest plumbing an HTTP-driven package needs land here too (lesson:
"A module's test conftest needs plumbing copied from an existing one"): the
autouse `_app_on_test_db` guard below, `_commit_pending_before_requests` on
every client fixture, and `from ... import name as name` for the gis fixtures
re-exported AND consumed here."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.models import MediaFile
from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.applications.permissions import APPLICATIONS_REVIEW, APPLICATIONS_VIEW_ANY
from app.modules.auth.models import Applicant, User
from app.modules.gis.models import Contour, GisLayer
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


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    """The app under test must open the TEST database, not the dev one. An
    autouse fixture applies only inside its own package, so importing gis's
    helpers above does NOT bring gis's own copy along — without this every
    request 401s, because `create_app()`'s lifespan opens the shared dev DB and
    none of the session rows the fixtures wrote are visible to it (lesson)."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def unique_pinfl() -> str:
    # Leading digit 1: 2/3/4/5/6/7/8/9 are already claimed by other test modules
    # sharing this same persistent test DB (see tests/modules/gis/conftest.py's
    # own comment on the same convention).
    return f"1{uuid.uuid4().int % 10**13:013d}"


@pytest.fixture
async def applicant(db: AsyncSession) -> Applicant:
    """A fully registered individual applicant, owned by a real user —
    `applications.applicant_id`/`submitted_by_user_id` are both NOT NULL FKs, so a
    bare `uuid7()` would fail the FK before whatever the test means to exercise."""
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    row = Applicant(kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id)
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def staff_user(db: AsyncSession) -> User:
    """A plain staff user for `set_status`'s `actor=` — distinct from
    `applicant`, whose own `owner_user_id` already covers the applicant-actor
    case. Not permission-bearing: `set_status` enforces no permission of its
    own (that is the CALLING module's router's job)."""
    return await make_user(db, role_code="executor_staff")


@pytest.fixture
async def grazing_activity_id(db: AsyncSession) -> uuid.UUID:
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'grazing'"))
    return rows.scalar_one()


@pytest.fixture
async def published_contour(
    db: AsyncSession, contours_layer: GisLayer, leshoz, approval_doc: MediaFile
) -> Contour:
    """A contour whose single version is published, at random coordinates (a fixed
    committed geometry accumulates across runs — lesson). `approval_doc_id` is
    mandatory: `ck_contour_versions_published_needs_doc` rejects a published
    version without one (mirrors norms's and gis's own `published_contour`)."""
    contour = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    await db.flush()
    return contour


@pytest.fixture
async def sheep_type_id(db: AsyncSession) -> uuid.UUID:
    """`sheep_goat_6m` — «Қўй ва эчки (6 ойдан катта)», seeded by migration 0005
    (`LIVESTOCK_TYPES`), never created here: `livestock_types` is a fixed
    catalogue, and inserting a private copy per test would leave rows in the
    shared, persistent test DB that `GET /refs/livestock-types` would then
    offer to a real form."""
    rows = await db.execute(text("SELECT id FROM livestock_types WHERE code = 'sheep_goat_6m'"))
    return rows.scalar_one()


async def _client_for_applicant(db: AsyncSession, user: User):
    """A signed-in client for an applicant USER — the applicant counterpart of
    gis's `_client_for`, which builds an `executor_staff` with personal grants
    and therefore cannot stand in for a citizen: `applications.create` is a ROLE
    grant on `applicant` (migration 0015), and `get_current_user` gates an
    applicant-role user that has no `applicants` row of its own to a short
    exempt-path list (`ERR-AUTH-008`), so the caller must pass a user that
    already owns one.

    `_commit_pending_before_requests` is what makes a fixture listed AFTER the
    client in a test's parameter list (`published_contour`, say) visible to the
    app's own connection (lesson)."""
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client


@pytest.fixture
async def applicant_client(db: AsyncSession, applicant: Applicant):
    """The owner of the `applicant` fixture's row, signed in.

    Deliberately built ON `applicant` rather than beside it: every later task
    needs the client and the `Applicant` row to be the same person — a
    submission is filed FOR `applicant` BY this user — and two independent
    fixtures would be two different applicants that only look related."""
    user = await db.get(User, applicant.owner_user_id)
    assert user is not None, "the `applicant` fixture always owns a real user"
    async for client in _client_for_applicant(db, user):
        yield client


@pytest.fixture
async def other_applicant_client(db: AsyncSession):
    """A SECOND, unrelated applicant — the stranger every ownership test needs.
    Its own user and its own `applicants` row, so nothing it does can be
    mistaken for the first applicant's."""
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    db.add(
        Applicant(kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id)
    )
    await db.flush()
    async for client in _client_for_applicant(db, user):
        yield client


@pytest.fixture
async def hodim_client(db: AsyncSession, leshoz: Organization):
    """The reviewer (tz/03's «ходим», role `executor_staff`) of the leshoz that
    owns `published_contour` — the two share the `leshoz` fixture, which pytest
    caches per test, so "the same zone" is a fact rather than a coincidence.

    Two permissions, and only one of them is the production role's: migration
    0015 grants `executor_staff` `applications.review` and grants
    `applications.view_any` to `prosecutor` alone. `view_any` is added here
    because later tasks drive this same client through routes that need it, and
    because the read rule accepts EITHER code (`service._holds_view_any`) —
    a hodim who may take an application into work must be able to read it.
    """
    async for client in _client_for(
        db, APPLICATIONS_REVIEW, APPLICATIONS_VIEW_ANY, organization_id=leshoz.id
    ):
        yield client


@pytest.fixture
async def other_zone_hodim_client(db: AsyncSession, other_leshoz: Organization):
    """The same reviewer shape, zoned to a DIFFERENT leshoz — the actor every
    territorial refusal is proven against. Same grants as `hodim_client` on
    purpose: what differs between the two is the zone and nothing else, so a
    test that passes for one and fails for the other can only be about
    territory."""
    async for client in _client_for(
        db, APPLICATIONS_REVIEW, APPLICATIONS_VIEW_ANY, organization_id=other_leshoz.id
    ):
        yield client
