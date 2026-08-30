"""Shared gis fixtures. Geometry is written as WKT and converted by PostGIS, so a
test never hand-builds GeoJSON; areas are real (a 0.01° x 0.01° box near Tashkent
is roughly 92 ha), which is what makes the area assertions meaningful."""

import json
import random
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.models import MediaFile
from app.core.time import business_today
from app.db import make_session_factory, uuid7
from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.auth.models import Applicant, User, UserPermission
from app.modules.gis import repo
from app.modules.gis.models import Contour, ContourVersion, GisLayer, LayerFeature
from app.modules.gis.permissions import CONTOURS_APPROVE, CONTOURS_MANAGE, LAYERS_MANAGE
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with
from tests.modules.auth.test_sessions import make_session, make_user


def box_wkt(min_lon: float, min_lat: float, size: float = 0.01) -> str:
    """A closed square polygon in WGS84 degrees."""
    x0, y0, x1, y1 = min_lon, min_lat, min_lon + size, min_lat + size
    return f"POLYGON(({x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}))"


def random_anchor() -> tuple[float, float]:
    """A random (lon, lat) pair, nowhere near the module's conventional
    box_wkt(69.9, 41.5) (and its 69.91/69.905 neighbours) or the "elsewhere"
    box_wkt(60.0, 41.5) — for a scenario that needs several boxes anchored
    relative to EACH OTHER (e.g. two adjoining fund polygons and a contour
    straddling their seam), not just one standalone box. A fixed
    'currently empty' spot is not good enough for that: box_wkt(69.9, 41.5)
    already carries several leftover published contours from
    test_contours_api.py's `published_contour` + `gis_client` combination,
    accumulated commit by commit across past test runs (confirmed empirically
    while building task 4) — a random spot cannot be poisoned by a fixed
    literal some other test committed, past or future."""
    return random.uniform(0.0, 40.0), random.uniform(0.0, 30.0)


def random_box_wkt() -> str:
    """A box_wkt-shaped box at a random, isolated spot — see `random_anchor`'s
    own note for why a fixed literal is not good enough here."""
    lon, lat = random_anchor()
    return box_wkt(lon, lat)


async def version_wkt(db: AsyncSession, version: ContourVersion) -> str:
    """A version's own geometry as WKT, read back from PostGIS — lets a sibling
    fixture (a restriction, a fire ban) sit exactly on top of wherever
    `draft_version` actually is without hard-coding a second, independent copy
    of its location that could drift apart from it."""
    wkt = await db.scalar(
        text("SELECT ST_AsText(geom) FROM contour_versions WHERE id = :id"), {"id": version.id}
    )
    assert wkt is not None
    return wkt


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
async def other_leshoz(db: AsyncSession) -> Organization:
    """A second organization, distinct from `leshoz` — the target of a
    cross-organization create in the zone test (final review, finding 1).
    `leshoz` is the actor's OWN zone there, so proving a mismatch is refused
    needs a second, different organization to attempt the create against; not
    a fixture `leshoz` itself can produce twice (pytest fixtures are cached per
    test, so requesting the same one twice gives the same instance, not two)."""
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
        name={"uz_cyrl": "Тест ЎХ 2", "ru": "Тестовый лесхоз 2"},
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


async def make_version(
    db: AsyncSession, contour_id: uuid.UUID, wkt: str, **over: Any
) -> ContourVersion:
    """A contour_version inserted directly (ORM), never through the API — the
    shared shape behind task 4's version fixtures below (draft or published, at
    whatever box the test needs). Mirrors `make_contour`'s pattern; `area_ha` is
    a fixed placeholder since none of the checks read it."""
    fields: dict[str, Any] = {
        "contour_id": contour_id,
        "version_no": 1,
        "geom": func.ST_Multi(func.ST_GeomFromText(wkt, 4326)),
        "area_ha": Decimal("92.0000"),
        "source": "survey",
        "status": "draft",
    }
    fields.update(over)
    version = ContourVersion(**fields)
    db.add(version)
    await db.flush()
    await db.refresh(version)
    return version


async def make_feature(db: AsyncSession, layer: GisLayer, wkt: str, **over: Any) -> LayerFeature:
    """A layer_features row inserted directly (ORM) — the layer_features
    endpoints arrive in task 6; a fixture that waited for them would be a
    circular dependency (task-4 brief, decision 2)."""
    fields: dict[str, Any] = {
        "layer_id": layer.id,
        "geom": func.ST_GeomFromText(wkt, 4326),
        "status": "published",
    }
    fields.update(over)
    feature = LayerFeature(**fields)
    db.add(feature)
    await db.flush()
    return feature


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


# --- Task 4: topology-check fixtures ------------------------------------------
#
# None of these ever call `db.commit()` — only `db.add`/`db.flush` through the
# `db` fixture's own session (tests/conftest.py), which rolls the whole
# transaction back after the test. That is deliberate, not an oversight: it is
# what keeps `published_fund_boundary_elsewhere` from poisoning every other
# test's `skipped` assertion in this shared, persistent test DB (task-4 brief,
# decision 3) — the row never survives past the test that created it, in this
# run or the next. Do not combine any of these fixtures with a `_client_for`
# based client (gis_client and friends): that would commit them for real
# (lesson: "A `_client_for`-style fixture's setup-time commit only covers what
# ran before it" — its request hook commits `db` unconditionally).


@pytest.fixture
async def draft_version(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization
) -> ContourVersion:
    """A clean draft version at a random, isolated location (`random_box_wkt`,
    not the module's conventional box_wkt(69.9, 41.5) — see that helper's own
    note) — every check should come back pass or skipped."""
    contour = await make_contour(db, contours_layer, leshoz)
    return await make_version(db, contour.id, random_box_wkt())


@pytest.fixture
async def neighbouring_published_contour(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, approval_doc: MediaFile
) -> ContourVersion:
    """A published contour at box_wkt(69.9, 41.5) — the 'other contour' the
    overlap check compares a draft version against (task-4 brief)."""
    contour = await make_contour(db, contours_layer, leshoz)
    return await make_version(
        db,
        contour.id,
        box_wkt(69.9, 41.5),
        status="published",
        approval_doc_id=approval_doc.id,
        published_at=func.now(),
    )


@pytest.fixture
async def draft_version_touching_it(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization
) -> ContourVersion:
    """Shares an edge with `neighbouring_published_contour` — a zero-area
    intersection, a border rather than an overlap (ruling 15)."""
    contour = await make_contour(db, contours_layer, leshoz)
    return await make_version(db, contour.id, box_wkt(69.91, 41.5))


@pytest.fixture
async def draft_version_overlapping_it(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization
) -> ContourVersion:
    """A half-step offset from `neighbouring_published_contour` — a real overlap,
    well above the tolerance."""
    contour = await make_contour(db, contours_layer, leshoz)
    return await make_version(db, contour.id, box_wkt(69.905, 41.5))


@pytest.fixture
async def published_restriction_over_it(
    db: AsyncSession, draft_version: ContourVersion
) -> LayerFeature:
    """A published `restrictions` feature over the exact box `draft_version`
    uses — read back from `draft_version`'s own geometry (`version_wkt`) rather
    than a second, independent literal, since `draft_version` itself now sits
    at a random location (see its own note)."""
    layer = await repo.layer_by_code(db, "restrictions")
    assert layer is not None
    return await make_feature(db, layer, await version_wkt(db, draft_version))


@pytest.fixture
async def expired_fire_ban_over_it(db: AsyncSession, draft_version: ContourVersion) -> LayerFeature:
    """A `fire_bans` feature whose validity period ended well before today.
    Built from `business_today()`, never `date.today()` (lesson) — the same
    "today" `run_checks` defaults `on_date` to, so the fixture and the check
    under test agree regardless of the server's own timezone."""
    layer = await repo.layer_by_code(db, "fire_bans")
    assert layer is not None
    today = business_today()
    return await make_feature(
        db,
        layer,
        await version_wkt(db, draft_version),
        valid_from=today - timedelta(days=400),
        valid_to=today - timedelta(days=370),
    )


@pytest.fixture
async def current_fire_ban_over_it(db: AsyncSession, draft_version: ContourVersion) -> LayerFeature:
    """A `fire_bans` feature whose validity period spans today."""
    layer = await repo.layer_by_code(db, "fire_bans")
    assert layer is not None
    today = business_today()
    return await make_feature(
        db,
        layer,
        await version_wkt(db, draft_version),
        valid_from=today - timedelta(days=10),
        valid_to=today + timedelta(days=10),
    )


@pytest.fixture
async def published_fund_boundary_elsewhere(db: AsyncSession) -> LayerFeature:
    """Ruling 9's `fail` branch needs a non-empty `forest_fund` layer — but the
    layer is empty in production today (task-4 brief, decision 3), and every
    OTHER test's `skipped` assertion depends on that staying true in the
    shared, persistent test DB. See the module note above this section: this
    fixture is never committed, so it cannot leak into any other test."""
    layer = await repo.layer_by_code(db, "forest_fund")
    assert layer is not None
    return await make_feature(db, layer, box_wkt(60.0, 41.5))


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


async def _client_for(
    db: AsyncSession, *permissions: str, organization_id: uuid.UUID | None = None
):
    """`organization_id`, when given, zones the actor to one organization
    (`User.organization_id`) instead of the zone-free default `signed_in_with`
    builds — `signed_in_with` itself has no parameter for this (it lives in
    `tests/modules/admin/test_organizations_admin.py`, shared by other modules'
    tests too), so the org-scoped path is built inline here rather than
    widening that shared helper for one gis-only case."""
    if organization_id is None:
        user, token, csrf = await signed_in_with(db, *permissions)
    else:
        user = await make_user(db, role_code="executor_staff", organization_id=organization_id)
        for code in permissions:
            db.add(UserPermission(user_id=user.id, permission_code=code))
        await db.flush()
        _, token, csrf = await make_session(db, user)
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
async def org_scoped_gis_client(db: AsyncSession, leshoz: Organization):
    """A `CONTOURS_MANAGE` actor zoned to `leshoz` — migration 0010 grants
    `gis.contours.manage` to `gis_specialist`, and a leshoz-level specialist has
    their own `organization_id` set, so this is the shape the zone check
    (final review, finding 1) actually has to defend against. `gis_client`
    stays zone-free (`organization_id=None`, republic-wide) on purpose, so the
    other 3.6a tests are unaffected by this addition."""
    async for client in _client_for(db, CONTOURS_MANAGE, organization_id=leshoz.id):
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
