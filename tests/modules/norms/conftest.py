"""Fixtures for the norms module.

Everything spatial is built with the helpers stage 3.6a already wrote —
`tests/modules/gis/conftest.py` exports `make_contour`, `make_version`,
`make_feature`, `box_wkt`, `random_box_wkt` and `_client_for` as plain functions
(its own fixtures import `signed_in_with`/`make_user` from the admin and auth
test modules the same way). Do NOT re-implement contour insertion in raw SQL
here: two ways to build a contour is how the two drift apart.

`make_version` writes a FIXED `area_ha` of 92.0000 ha, which is exactly what this
stage wants — every MaxSB assertion below is then a plain number, not a value
read back from PostGIS."""

import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.models import MediaFile
from app.db import uuid7
from app.modules.auth.models import User
from app.modules.gis.models import Contour, GisLayer
from app.modules.norms.permissions import (
    NORMS_APPROVE,
    NORMS_MANAGE,
    NORMS_PUBLISH,
    TARIFFS_MANAGE,
    TARIFFS_PUBLISH,
)
from tests.modules.auth.test_sessions import make_user
from tests.modules.gis.conftest import (
    _client_for,
    applicant_client,  # noqa: F401 — a fixture imported into a conftest IS available
    box_wkt,  # noqa: F401 — re-exported for a future fixed-geometry test
    gis_user,  # noqa: F401 — the `created_by` FK target the tests below use
    make_contour,
    make_version,
    random_box_wkt,
)
from tests.modules.gis.conftest import (
    approval_doc as approval_doc,  # re-exported AND used as a parameter name below
)
from tests.modules.gis.conftest import (
    contours_layer as contours_layer,  # re-exported AND used as a parameter name below
)
from tests.modules.gis.conftest import (
    leshoz as leshoz,  # re-exported AND used as a parameter name below
)
from tests.modules.gis.conftest import (
    other_leshoz as other_leshoz,  # re-exported AND used as a parameter name below
)

CONTOUR_AREA_HA = Decimal("92.0000")  # what make_version writes


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    """The same guard `tests/modules/gis/conftest.py` and
    `tests/modules/notifications/conftest.py` carry: the app under test must open
    the TEST database, not the dev one. An autouse fixture applies only inside
    its own package, so importing gis's does NOT bring this along — it has to be
    declared here, or every route test in this package writes to the dev DB."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def unique_suffix() -> str:
    """Collision-free discriminator for codes: the test database is shared and
    persistent (lesson), and this stage's EXCLUDE constraints turn a leaked row
    from an earlier run into a period conflict in an unrelated test."""
    return uuid.uuid4().hex[:8]


@pytest.fixture
async def published_contour(
    db: AsyncSession, contours_layer: GisLayer, leshoz, approval_doc: MediaFile
) -> Contour:
    """A contour whose single version is published, at random coordinates (a
    fixed committed geometry accumulates across runs — lesson). `approval_doc_id`
    is mandatory: `ck_contour_versions_published_needs_doc` rejects a published
    version without one (mirrors gis's own `published_contour` fixture)."""
    contour = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    await db.flush()
    return contour


@pytest.fixture
async def checker_user(db: AsyncSession) -> User:
    """A second real user, distinct from `gis_user` — `approved_by` is a real
    FK to `users.id` (not just a maker-checker CHECK target), so a random
    `uuid7()` fails the FK before the invariant under test ever runs."""
    return await make_user(db)


@pytest.fixture
async def draft_only_contour(db: AsyncSession, contours_layer: GisLayer, leshoz) -> Contour:
    contour = await make_contour(db, contours_layer, leshoz)
    await make_version(db, contour.id, random_box_wkt(), status="draft")
    await db.flush()
    return contour


@pytest.fixture
async def survey_doc(db: AsyncSession) -> MediaFile:
    """The geobotanical survey a norm comes out of (VMQ 689) — a second
    MediaFile row beside `approval_doc`, since a norm carries two distinct
    documents: the survey it was computed from and the decree that put it in
    force."""
    doc = MediaFile(
        storage_key=f"t/{uuid.uuid4().hex}",
        filename="geobotanic-survey.pdf",
        content_type="application/pdf",
        size_bytes=100,
        sha256="1" * 64,
    )
    db.add(doc)
    await db.flush()
    return doc


@pytest.fixture
async def grazing_activity_id(db: AsyncSession) -> uuid.UUID:
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'grazing'"))
    return rows.scalar_one()


@pytest.fixture
async def haymaking_activity_id(db: AsyncSession) -> uuid.UUID:
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'haymaking'"))
    return rows.scalar_one()


@pytest.fixture
async def science_activity_id(db: AsyncSession) -> AsyncIterator[uuid.UUID]:
    """`science` has zero seeded tariff rows (`test_science_has_no_tariff` —
    VMQ 278's annex has no rate for it), unlike every other activity, which is
    already published open-ended from 2015-09-30. A test that needs to publish
    a FRESH tariff without tripping `ex_tariffs_one_in_force` uses this key —
    but `tariffs` has no delete-via-API (archived rows still count towards
    `test_science_has_no_tariff`'s absolute-zero assertion, and a row a failed
    permission check deliberately leaves PUBLISHED would otherwise conflict
    with every later run's own attempt to publish another one), so this
    fixture deletes every tariff row for this activity_type_id at teardown,
    regardless of who created it — nothing else in the product is ever supposed
    to write one, and the delete is scoped to this one FK, never a blanket
    `DELETE FROM tariffs`."""
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'science'"))
    activity_id = rows.scalar_one()
    yield activity_id
    await db.execute(text("DELETE FROM tariffs WHERE activity_type_id = :id"), {"id": activity_id})
    await db.commit()


# --- clients ---------------------------------------------------------------
# `_client_for(db, *permissions, organization_id=None)` is an async generator
# (see gis/conftest.py): drive it with `async for`, exactly as the gis fixtures
# do. Permissions are POSITIONAL — a keyword `permissions=[...]` would silently
# build a client with none and produce a very confusing 403.


@pytest.fixture
async def gis_specialist_client(db: AsyncSession, leshoz) -> AsyncIterator[httpx.AsyncClient]:
    """Drafts norms for its own leshoz. Zoned, because zone scoping is not a
    permission check (lesson) and every write path here re-applies it."""
    async for client in _client_for(db, NORMS_MANAGE, organization_id=leshoz.id):
        yield client


@pytest.fixture
async def other_zone_specialist_client(
    db: AsyncSession, other_leshoz
) -> AsyncIterator[httpx.AsyncClient]:
    async for client in _client_for(db, NORMS_MANAGE, organization_id=other_leshoz.id):
        yield client


@pytest.fixture
async def leadership_client(db: AsyncSession, leshoz) -> AsyncIterator[httpx.AsyncClient]:
    """The raҳbar: approves, never publishes (ruling 16)."""
    async for client in _client_for(db, NORMS_APPROVE, NORMS_MANAGE, organization_id=leshoz.id):
        yield client


@pytest.fixture
async def central_admin_client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """The central office: zone-free, publishes norms and parameters."""
    async for client in _client_for(db, NORMS_PUBLISH):
        yield client


@pytest.fixture
async def tariffs_maker_client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    async for client in _client_for(db, TARIFFS_MANAGE):
        yield client


@pytest.fixture
async def tariffs_checker_client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    async for client in _client_for(db, TARIFFS_PUBLISH, TARIFFS_MANAGE):
        yield client


# `applicant_client` is imported above rather than rewritten: an applicant-role
# user needs its own `applicants` row or `get_current_user` gates it to the
# registration paths (ERR-AUTH-008), and every calculation request would answer
# 403 for a reason that has nothing to do with this stage.


@pytest.fixture
async def published_coef_sb(db: AsyncSession) -> None:
    """Publishes the ten seeded `coef_sb:*` drafts (ruling 8) for the tests that
    need a grazing calculation to succeed. Scoped by code, never a blanket
    UPDATE — the test database is shared (lesson)."""
    await db.execute(
        text(
            "UPDATE rule_parameters SET status = 'published' "
            "WHERE code LIKE 'coef_sb:%' AND status = 'draft'"
        )
    )
    await db.flush()


@pytest.fixture
def param_row(db: AsyncSession):
    async def _insert(
        code: str,
        value: str,
        *,
        effective_from: date = date(2020, 1, 1),
        effective_to: date | None = None,
        status: str = "published",
    ) -> uuid.UUID:
        param_id = uuid7()
        await db.execute(
            text(
                "INSERT INTO rule_parameters "
                "(id, code, value, effective_from, effective_to, basis, status) "
                "VALUES (:id, :code, to_jsonb(:value::text), :ef, :et, 'test', :status)"
            ).bindparams(
                id=param_id,
                code=code,
                value=value,
                ef=effective_from,
                et=effective_to,
                status=status,
            )
        )
        await db.flush()
        return param_id

    return _insert
