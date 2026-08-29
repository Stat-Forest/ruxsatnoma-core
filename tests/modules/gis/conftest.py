"""Shared gis fixtures. Geometry is written as WKT and converted by PostGIS, so a
test never hand-builds GeoJSON; areas are real (a 0.01° x 0.01° box near Tashkent
is roughly 92 ha), which is what makes the area assertions meaningful."""

import json
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.models import MediaFile
from app.db import make_session_factory, uuid7
from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.auth.models import Applicant, User
from app.modules.gis.models import Contour, ContourVersion, GisLayer
from app.modules.gis.permissions import CONTOURS_APPROVE, CONTOURS_MANAGE, LAYERS_MANAGE
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with
from tests.modules.auth.test_sessions import make_session, make_user


def box_wkt(min_lon: float, min_lat: float, size: float = 0.01) -> str:
    """A closed square polygon in WGS84 degrees."""
    x0, y0, x1, y1 = min_lon, min_lat, min_lon + size, min_lat + size
    return f"POLYGON(({x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}))"


@pytest.fixture
async def contours_layer(db: AsyncSession) -> GisLayer:
    layer = (await db.execute(select(GisLayer).where(GisLayer.code == "contours"))).scalar_one()
    return layer


@pytest.fixture
async def leshoz(db: AsyncSession) -> Organization:
    """An organization of our own, so tests never collide on the shared test DB.

    `ck_organizations_root_is_agency` requires a non-agency row to carry a
    parent, and the single-agency partial unique index makes the agency row a
    singleton another test module may already have committed to the shared,
    persistent test DB — so this reuses one if it exists rather than assuming
    a fresh database (lesson: "the test database is shared, persistent").
    """
    agency = (
        await db.execute(select(Organization).where(Organization.kind == "agency"))
    ).scalar_one_or_none()
    if agency is None:
        agency = Organization(
            id=uuid7(),
            code=f"A{uuid.uuid4().hex[:8]}",
            name={"uz_cyrl": "Тест агентлиги", "ru": "Тестовое агентство"},
            kind="agency",
        )
        db.add(agency)
        await db.flush()
    org = Organization(
        id=uuid7(),
        code=f"T{uuid.uuid4().hex[:8]}",
        name={"uz_cyrl": "Тест ЎХ", "ru": "Тестовый лесхоз"},
        kind="leshoz",
        parent_id=agency.id,
    )
    db.add(org)
    await db.flush()
    return org


@pytest.fixture
async def approval_doc(db: AsyncSession) -> MediaFile:
    """An active media_files row standing in for a basis document (decree, act) —
    what a published contour version's approval_doc_id CHECK requires (ruling 3)."""
    doc = MediaFile(
        storage_key=f"t/{uuid.uuid4().hex}",
        filename="approval.pdf",
        content_type="application/pdf",
        size_bytes=100,
        sha256="0" * 64,
    )
    db.add(doc)
    await db.flush()
    return doc


async def make_contour(
    db: AsyncSession, layer: GisLayer, org: Organization, **over: Any
) -> Contour:
    fields: dict[str, Any] = {
        "id": uuid7(),
        "layer_id": layer.id,
        "organization_id": org.id,
        "number": f"C{uuid.uuid4().hex[:8]}",
        "kind": "contour",
        "status": "active",
    }
    fields.update(over)
    contour = Contour(**fields)
    db.add(contour)
    await db.flush()
    return contour


@pytest.fixture
async def gis_user(db: AsyncSession) -> User:
    """Owns rows created directly (bypassing the API) by other gis fixtures and
    by test_geometry.py's repo-level tests — a `created_by` FK target, nothing
    permission-bearing (repo functions enforce no permissions of their own)."""
    return await make_user(db)


async def wkt_to_geojson(db: AsyncSession, wkt: str) -> dict[str, Any]:
    """PostGIS's own WKT->GeoJSON, so a test's geometry is defined once as WKT
    (matching box_wkt) and converted through the same engine `insert_version`
    itself relies on — never a hand-rolled GeoJSON literal that could silently
    drift from what box_wkt actually describes."""
    raw = await db.scalar(text("SELECT ST_AsGeoJSON(ST_GeomFromText(:wkt, 4326))"), {"wkt": wkt})
    assert raw is not None
    return json.loads(raw)


@pytest.fixture
async def published_contour(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, approval_doc: MediaFile
) -> ContourVersion:
    """A contour with a version already `published` — inserted directly (ORM),
    never through the API: the publish endpoint is Task 5's, not built yet, and
    a fixture depending on a later task's endpoint would be a circular
    dependency. `approval_doc_id` is mandatory here — `published_needs_doc`
    rejects a published row without one."""
    contour = await make_contour(db, contours_layer, leshoz)
    version = ContourVersion(
        contour_id=contour.id,
        version_no=1,
        geom=func.ST_Multi(func.ST_GeomFromText(box_wkt(69.9, 41.5), 4326)),
        area_ha=Decimal("92.0000"),
        source="survey",
        status="published",
        approval_doc_id=approval_doc.id,
        published_at=func.now(),
    )
    db.add(version)
    await db.flush()
    await db.refresh(version)
    return version


# --- Signed-in client fixtures for the gis HTTP API (task 2) -----------------
#
# Thin wrappers over helpers this codebase already has: make_user/make_session
# (tests/modules/auth/test_sessions.py) and signed_in_with/auth_client
# (tests/modules/admin/test_organizations_admin.py) build the user and the
# session cookies; make_client (tests/conftest.py) drives a real app instance.
# Task 3+ fixtures needing a different permission mix are added here, not in a
# second file.


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    """Same guard as tests/modules/notifications/conftest.py: the app under test
    must open the TEST database, not the dev one."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _restore_gis_layers(engine):
    """gis_layers is a small, FIXED catalogue (ruling 19) seeded once by
    migration 0010 — never created or deleted, only its 3 presentation columns
    ever change. `PATCH /gis/layers/{code}` mutates a row through the APP's own
    session (`_client_for` above commits it explicitly, on the engine
    `create_app()` builds inside its own lifespan) — NOT the `db` fixture's
    session, so `db`'s teardown `rollback()` can never undo it, and a test that
    patches a layer would otherwise leave the mutation in the shared, persistent
    test DB forever (task-2 review, finding 1). This is why the restore below
    also avoids `db`: committing on that session would additionally commit
    whatever the test body itself left pending on it, silently widening what
    the test is supposed to roll back.

    Snapshots every row's style/is_public/status before the test on a session
    of its own, restores them after through an explicit commit on another, and
    re-reads to confirm the restore actually landed rather than trusting it
    silently — so a regression here fails loudly (an assertion error in this
    fixture's teardown) instead of corrupting the catalogue for whichever test
    or task runs next.
    """
    factory = make_session_factory(engine)
    async with factory() as session:
        before = (
            await session.execute(
                select(GisLayer.id, GisLayer.style, GisLayer.is_public, GisLayer.status)
            )
        ).all()
    snapshot = {row.id: (row.style, row.is_public, row.status) for row in before}
    yield
    async with factory() as session:
        for layer_id, (style, is_public, status) in snapshot.items():
            await session.execute(
                update(GisLayer)
                .where(GisLayer.id == layer_id)
                .values(style=style, is_public=is_public, status=status)
            )
        await session.commit()
        after = (
            await session.execute(
                select(GisLayer.id, GisLayer.style, GisLayer.is_public, GisLayer.status)
            )
        ).all()
    restored = {row.id: (row.style, row.is_public, row.status) for row in after}
    assert restored == snapshot, "gis_layers catalogue was not fully restored after the test"


def unique_pinfl() -> str:
    # Leading digit 2: 3/4/5/6/7 are already claimed by other test modules
    # sharing this same persistent test DB (see tests/core/test_files_api.py's
    # own comment on the same convention).
    return f"2{uuid.uuid4().int % 10**13:013d}"


def _commit_pending_before_requests(client: httpx.AsyncClient, db: AsyncSession) -> None:
    """pytest instantiates a test's fixtures in the left-to-right order of its
    parameter list — e.g. `test_x(gis_client, leshoz)` sets `gis_client` up (and
    its own commit below) BEFORE `leshoz` even runs. `leshoz` (and any other
    fixture writing through the shared `db` session) is then only `flush()`ed,
    not committed, when the test body's first request fires — invisible to the
    app's own session, a different connection, until committed (confirmed
    empirically: a fixture listed after a `_client_for`-based client is NOT
    visible to a fresh connection at that point). Committing again right before
    every outgoing request picks up whatever else `db` was given in the
    meantime, regardless of fixture order — cheap, and a no-op commit when
    there is nothing new to see."""

    async def _commit_pending(request: httpx.Request) -> None:
        await db.commit()

    client.event_hooks["request"] = [*client.event_hooks.get("request", []), _commit_pending]


async def _client_for(db: AsyncSession, *permissions: str):
    user, token, csrf = await signed_in_with(db, *permissions)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client


@pytest.fixture
async def gis_client(db: AsyncSession):
    """The GIS specialist: draws, edits and imports, but never approves."""
    async for client in _client_for(db, CONTOURS_MANAGE, LAYERS_MANAGE):
        yield client


@pytest.fixture
async def rahbar_client(db: AsyncSession):
    """The approver: approves and publishes, does not draw."""
    async for client in _client_for(db, CONTOURS_APPROVE):
        yield client


@pytest.fixture
async def applicant_client(db: AsyncSession):
    """A fully registered applicant (role_code="applicant" WITH its own
    `Applicant` row) — not a grantless executor_staff user standing in for one.

    `signed_in_with(db)` with no codes would authenticate fine too (no gis route
    checks a permission an applicant lacks by construction), but get_current_user
    (app/modules/auth/deps.py) additionally gates any applicant-role user that
    has no linked Applicant row to a short exempt-path list (ERR-AUTH-008) that
    does not include /gis/*, and later gis read rules (contours, applications)
    turn on the role itself, not just held permissions — so the fixture must be
    a real, fully registered applicant. Shape mirrors
    tests/core/test_files_api.py::registered_applicant.
    """
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    db.add(
        Applicant(kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id)
    )
    await db.flush()
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client
