"""Shared gis fixtures. Geometry is written as WKT and converted by PostGIS, so a
test never hand-builds GeoJSON; areas are real (a 0.01° x 0.01° box near Tashkent
is roughly 92 ha), which is what makes the area assertions meaningful."""

import hashlib
import json
import random
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import shapely
from pyogrio.raw import write
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.core import storage
from app.core.models import MediaFile
from app.core.time import business_today
from app.db import make_session_factory, uuid7
from app.main import create_app
from app.modules.admin.models import Organization, Region
from app.modules.auth.models import Applicant, User, UserPermission
from app.modules.gis import import_service, repo
from app.modules.gis.models import Contour, ContourVersion, GisImport, GisLayer, LayerFeature
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
async def restrictions_polygon(db: AsyncSession) -> dict[str, Any]:
    """A polygon GeoJSON at a random, isolated location (`random_box_wkt`, never
    the module's conventional box_wkt(69.9, 41.5) spot) — task 6's own tests
    PUBLISH real `restrictions`/`protection`/`fire_bans` rows through the API
    (a `_client_for` client commits for real), so this must stay clear of every
    coordinate a Task 4/5 check test depends on remaining empty or predictable
    (lesson: 'A fixed test geometry that a _client_for client commits
    accumulates forever' — task-6 controller, decision 2)."""
    return await wkt_to_geojson(db, random_box_wkt())


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


# --- Task 5: lifecycle fixtures ------------------------------------------
#
# Unlike the task-4 fixtures just above, every one of these gets combined with
# a `_client_for`-based client (`gis_client`/`rahbar_client` and friends) in
# its own test, which commits it for real (lesson: "A `_client_for`-style
# fixture's setup-time commit only covers what ran before it"). Any of these
# that ends up `published` therefore sticks around forever in the shared,
# persistent test DB — exactly the shape "A fixed test geometry that a
# `_client_for` client commits accumulates forever" warns about — so every one
# is anchored with `random_box_wkt()`/`random_anchor()`, never the module's
# conventional box_wkt(69.9, 41.5) spot or its neighbours (decision 2 from the
# task-5 controller).


@pytest.fixture
async def contour_with_draft(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization
) -> tuple[uuid.UUID, uuid.UUID]:
    """A fresh contour with one `draft` version — the lifecycle's own starting
    point, ready to run submit-review -> approve -> publish end to end."""
    contour = await make_contour(db, contours_layer, leshoz)
    version = await make_version(db, contour.id, random_box_wkt())
    return contour.id, version.id


@pytest.fixture
async def contour_in_review(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization
) -> tuple[uuid.UUID, uuid.UUID]:
    """A version already in `review` — the precondition `approve` needs, for
    the tests that stop there and never reach publish (a permission 403, a
    zone 403, and a missing-document 422)."""
    contour = await make_contour(db, contours_layer, leshoz)
    version = await make_version(db, contour.id, random_box_wkt(), status="review")
    return contour.id, version.id


@pytest.fixture
async def contour_with_two_versions(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, approval_doc: MediaFile
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """One contour, two versions: the first already `published`, the second
    `approved` and ready to replace it — the exact shape `publish_version`'s
    archive-the-previous step needs. Both sit at the same random, isolated
    anchor; `_overlap`'s own candidate query excludes a version from every
    OTHER version of its OWN contour regardless of location
    (`ov.contour_id <> ...`), so sharing one location is safe and the random
    anchor is only to stay clear of whatever a previous run left behind
    elsewhere."""
    wkt = random_box_wkt()
    contour = await make_contour(db, contours_layer, leshoz)
    first = await make_version(
        db,
        contour.id,
        wkt,
        status="published",
        approval_doc_id=approval_doc.id,
        published_at=func.now(),
    )
    second = await make_version(
        db,
        contour.id,
        wkt,
        version_no=2,
        status="approved",
        approval_doc_id=approval_doc.id,
    )
    return contour.id, first.id, second.id


@pytest.fixture
async def approved_version_overlapping_a_published_one(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, approval_doc: MediaFile
) -> tuple[uuid.UUID, uuid.UUID]:
    """An `approved` version that overlaps a DIFFERENT contour's already-
    published one — the one deliberate collision this task needs (decision 2
    from the task-5 controller). Both geometries come from a single random
    anchor plus a fixed 0.005-degree offset — the same half-step
    `draft_version_overlapping_it` (task 4) uses against
    `neighbouring_published_contour`, just anchored randomly instead of at the
    module's conventional box_wkt(69.9, 41.5) — so the only thing this version
    ever overlaps is the published row this fixture creates alongside it,
    never whatever an earlier run of this suite left behind elsewhere in the
    shared, persistent test DB.
    """
    lon, lat = random_anchor()
    published = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db,
        published.id,
        box_wkt(lon, lat),
        status="published",
        approval_doc_id=approval_doc.id,
        published_at=func.now(),
    )
    contour = await make_contour(db, contours_layer, leshoz)
    version = await make_version(
        db,
        contour.id,
        box_wkt(lon + 0.005, lat),
        status="approved",
        approval_doc_id=approval_doc.id,
    )
    return contour.id, version.id


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
    db: AsyncSession,
    *permissions: str,
    organization_id: uuid.UUID | None = None,
    region_id: uuid.UUID | None = None,
):
    """`organization_id`/`region_id`, when given, zone the actor to that
    organization/region (`User.organization_id`/`User.region_id`) instead of
    the zone-free default `signed_in_with` builds — `signed_in_with` itself
    has no parameter for this (it lives in
    `tests/modules/admin/test_organizations_admin.py`, shared by other modules'
    tests too), so the zone-scoped path is built inline here rather than
    widening that shared helper for one gis-only case. The two are
    independent, matching `Zone`'s own three independent axes
    (`app/core/abac.py`) — a caller can set one, the other, or (not needed
    today) both."""
    if organization_id is None and region_id is None:
        user, token, csrf = await signed_in_with(db, *permissions)
    else:
        user = await make_user(
            db, role_code="executor_staff", organization_id=organization_id, region_id=region_id
        )
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
async def org_scoped_layers_client(db: AsyncSession, leshoz: Organization):
    """A `LAYERS_MANAGE` actor zoned to `leshoz` — the shape the layer-feature
    zone rule (task-6 controller, decision 3) has to defend against: without
    it, a leshoz-level specialist could publish a republic-wide (no
    `organization_id`) fire ban through their own zone-scoped account. Mirrors
    `org_scoped_gis_client`'s own reasoning, one permission set over."""
    async for client in _client_for(db, LAYERS_MANAGE, organization_id=leshoz.id):
        yield client


@pytest.fixture
async def region_scoped_layers_client(db: AsyncSession):
    """A `LAYERS_MANAGE` actor zoned to a REGION but no organization — the
    shape `_assert_feature_zone`'s republic-wide gate originally missed
    (task-6 final review): checking `organization_id` alone let an actor
    scoped to a region (or district) reach a nominally republic-wide feature.
    `Region.code == "fergana"` is the same known-seeded row (migration 0005)
    admin's own tests already key off, e.g.
    `tests/modules/admin/test_refs_api.py`."""
    region_id = (await db.execute(select(Region.id).where(Region.code == "fergana"))).scalar_one()
    async for client in _client_for(db, LAYERS_MANAGE, region_id=region_id):
        yield client


@pytest.fixture
async def rahbar_client(db: AsyncSession):
    """The approver: approves and publishes, does not draw."""
    async for client in _client_for(db, CONTOURS_APPROVE):
        yield client


@pytest.fixture
async def org_scoped_rahbar_client(db: AsyncSession, other_leshoz: Organization):
    """A `CONTOURS_APPROVE` actor zoned to `other_leshoz` — the shape the zone
    check on the four lifecycle actions (submit-review/approve/publish/archive)
    has to defend against (task-5 controller, decision 6: every one of them is
    zone-scoped, not only permission-gated). Mirrors `org_scoped_gis_client`'s
    own reasoning, one permission set over."""
    async for client in _client_for(db, CONTOURS_APPROVE, organization_id=other_leshoz.id):
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


# --- Task 7: import fixtures --------------------------------------------------
#
# Unlike the task-4 fixtures above, every one of these COMMITS: the import job
# runs in a session of its own (`process_pending(factory)`), so nothing it must
# see may sit unflushed in the `db` fixture's transaction. The rows therefore
# survive in the shared, persistent test DB — which is safe here because each
# fixture builds its own fresh `leshoz` (so contour numbers never collide across
# runs) and anchors its geometry with `random_box_wkt()` (lesson: "A fixed test
# geometry that a `_client_for` client commits accumulates forever").


@pytest.fixture
def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    """What the workers get in production — `app.workers.runner` builds exactly
    this and hands it to every job."""
    return make_session_factory(engine)


def geojson_bytes(features: list[tuple[str | None, dict[str, Any]]]) -> bytes:
    """A FeatureCollection from `(wkt_or_None, properties)` pairs. A `None`
    geometry is the deliberate bad row of the atomicity tests; the WKT is
    converted here by shapely rather than by PostGIS, because these bytes have
    to be a real file on disk before any session exists."""
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": None
                    if wkt is None
                    else json.loads(shapely.to_geojson(shapely.from_wkt(wkt))),
                    "properties": properties,
                }
                for wkt, properties in features
            ],
        }
    ).encode()


def geopackage_bytes(
    tmp_path: Path, features: list[tuple[str, dict[str, Any]]], *, crs: str
) -> bytes:
    """A real GeoPackage in the given projection — the only format in this suite
    that carries a CRS other than 4326 without a sidecar (GeoJSON is 4326 by
    specification, and a shapefile needs its whole .prj/.dbf/.shx entourage
    zipped alongside). Written with pyogrio, so it is a genuine GDAL source, not
    a fixture that merely resembles one."""
    names = list(features[0][1])
    write(
        str(tmp_path / "layer.gpkg"),
        geometry=shapely.to_wkb(np.array([shapely.from_wkt(wkt) for wkt, _ in features])),
        field_data=[
            np.array([properties[name] for _, properties in features], dtype=object)
            for name in names
        ],
        fields=np.array(names, dtype=object),
        geometry_type="Polygon",
        crs=crs,
        driver="GPKG",
    )
    return (tmp_path / "layer.gpkg").read_bytes()


async def make_media_file(db: AsyncSession, data: bytes, *, filename: str, content_type: str):
    """A `media_files` row whose object really exists in MinIO — `run_import`
    reads the bytes back through `core.storage.get_object`, so a row alone is
    not enough."""
    file = MediaFile(
        id=uuid7(),
        storage_key=f"test/{uuid.uuid4().hex}",
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    await storage.put_object(file.storage_key, data, content_type)
    db.add(file)
    await db.flush()
    return file


async def make_import(
    db: AsyncSession,
    *,
    layer: GisLayer,
    org: Organization,
    started_by: User,
    data: bytes,
    fmt: str = "geojson",
    content_type: str = "application/geo+json",
    attribute_map: dict[str, Any] | None = None,
) -> GisImport:
    file = await make_media_file(db, data, filename=f"import.{fmt}", content_type=content_type)
    doc = await make_media_file(
        db, b"%PDF-1.4 decree", filename="decree.pdf", content_type="application/pdf"
    )
    row = GisImport(
        id=uuid7(),
        layer_id=layer.id,
        organization_id=org.id,
        file_id=file.id,
        approval_doc_id=doc.id,
        format=fmt,
        attribute_map=attribute_map if attribute_map is not None else {"number": "number"},
        status="pending",
        started_by=started_by.id,
    )
    db.add(row)
    await db.flush()
    await db.commit()  # the job runs in its OWN session and cannot see uncommitted rows
    return row


@pytest.fixture
async def pending_import(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, gis_user: User
) -> GisImport:
    """Two clean polygons with a number each — the happy path."""
    return await make_import(
        db,
        layer=contours_layer,
        org=leshoz,
        started_by=gis_user,
        data=geojson_bytes(
            [
                (random_box_wkt(), {"number": "14510q"}),
                (random_box_wkt(), {"number": "14511q"}),
            ]
        ),
    )


@pytest.fixture
async def pending_import_with_a_broken_row(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, gis_user: User
) -> GisImport:
    """A feature with no geometry at all — the parser reports it and nothing is
    written (ruling 7: an import is atomic)."""
    return await make_import(
        db,
        layer=contours_layer,
        org=leshoz,
        started_by=gis_user,
        data=geojson_bytes(
            [(random_box_wkt(), {"number": "14512q"}), (None, {"number": "14513q"})]
        ),
    )


@pytest.fixture
async def pending_import_missing_number(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, gis_user: User
) -> GisImport:
    """Row 1 carries no contour number (a shapefile writes a NULL text field as
    an empty string, which is what this reproduces). Caught in the pure-Python
    mapping pass, before any write — so the whole file's attribute problems are
    reported at once instead of one per run."""
    return await make_import(
        db,
        layer=contours_layer,
        org=leshoz,
        started_by=gis_user,
        data=geojson_bytes(
            [(random_box_wkt(), {"number": "14514q"}), (random_box_wkt(), {"number": ""})]
        ),
    )


@pytest.fixture
async def pending_import_non_polygon_on_row_1(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, gis_user: User
) -> GisImport:
    """Row 0 is a clean polygon and row 1 is a LINESTRING — real geometry the
    parser happily reads, which `insert_version`'s
    `ST_CollectionExtract(..., 3)` then reduces to nothing (ruling 10). The
    failure therefore happens HALFWAY THROUGH THE WRITES, which is the case the
    savepoint actually exists for: the attribute fixtures above never reach
    them."""
    lon, lat = random_anchor()
    return await make_import(
        db,
        layer=contours_layer,
        org=leshoz,
        started_by=gis_user,
        data=geojson_bytes(
            [
                (box_wkt(lon, lat), {"number": "14517q"}),
                (f"LINESTRING({lon} {lat}, {lon + 0.01} {lat + 0.01})", {"number": "14518q"}),
            ]
        ),
    )


@pytest.fixture
async def pending_import_restrictions(
    db: AsyncSession, leshoz: Organization, gis_user: User
) -> GisImport:
    """A non-contour layer: ruling 13 says the UNMAPPED attributes go into
    `props jsonb`, which is what that column is for — the narrow
    number/declared-area mapping is a CONTOUR-layer rule, not a global one."""
    layer = (await db.execute(select(GisLayer).where(GisLayer.code == "restrictions"))).scalar_one()
    return await make_import(
        db,
        layer=layer,
        org=leshoz,
        started_by=gis_user,
        data=geojson_bytes(
            [(random_box_wkt(), {"title": "Water protection zone", "note": "SanPiN", "rank": 2})]
        ),
        attribute_map={"name": "title"},
    )


@pytest.fixture
async def pending_import_area_mismatch(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, gis_user: User
) -> GisImport:
    """Declared 2.6 ha against a box that really is ~92 — 36 of Burchmulla's 151
    features look like this (ruling 2), and they must import anyway."""
    return await make_import(
        db,
        layer=contours_layer,
        org=leshoz,
        started_by=gis_user,
        data=geojson_bytes([(random_box_wkt(), {"number": "14516q", "area_ha": 2.6})]),
        attribute_map={"number": "number", "declared_area_ha": "area_ha"},
    )


@pytest.fixture
async def pending_import_duplicate_numbers(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, gis_user: User
) -> GisImport:
    """Both rows carry the same number in the source — several tenants share one
    contour in the real file (92 distinct numbers across 151 features)."""
    return await make_import(
        db,
        layer=contours_layer,
        org=leshoz,
        started_by=gis_user,
        data=geojson_bytes(
            [(random_box_wkt(), {"number": "14515q"}), (random_box_wkt(), {"number": "14515q"})]
        ),
    )


@pytest.fixture
async def pending_import_many_missing_numbers(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, gis_user: User
) -> GisImport:
    """Twelve rows, every one of them missing its contour number — the usual
    shape of a wrong attribute map, and the case whose report has to be capped
    (the test lowers `MAX_REPORT_ROWS` rather than building a real million-row
    file)."""
    return await make_import(
        db,
        layer=contours_layer,
        org=leshoz,
        started_by=gis_user,
        data=geojson_bytes([(random_box_wkt(), {"number": ""}) for _ in range(12)]),
    )


@pytest.fixture
async def pending_import_many_duplicate_numbers(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, gis_user: User
) -> GisImport:
    """Twelve rows sharing one number: one clean insert and eleven
    `duplicate_number` WARNINGS, which import fine (ruling 7) — the case that
    proves the warnings list is bounded too, not just the error list."""
    return await make_import(
        db,
        layer=contours_layer,
        org=leshoz,
        started_by=gis_user,
        data=geojson_bytes([(random_box_wkt(), {"number": "14519q"}) for _ in range(12)]),
    )


@pytest.fixture
async def pending_import_many_org_mismatches(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, gis_user: User
) -> GisImport:
    """Eight rows, each naming a DIFFERENT leshoz, none of them the organization
    the request names — what pointing `organization_name` at a high-cardinality
    column does (ruling 12: the name is compared, never matched on). Every row
    imports; every distinct name is its own warning."""
    return await make_import(
        db,
        layer=contours_layer,
        org=leshoz,
        started_by=gis_user,
        data=geojson_bytes(
            [(random_box_wkt(), {"number": f"1452{i}q", "leshoz": f"Leshoz {i}"}) for i in range(8)]
        ),
        attribute_map={"number": "number", "organization_name": "leshoz"},
    )


@pytest.fixture
async def pending_import_utm42(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, gis_user: User, tmp_path: Path
) -> GisImport:
    """A GeoPackage in UTM zone 42N — metres, not degrees. Nothing in this
    fixture converts it; PostGIS does, at insert (decision #13)."""
    return await make_import(
        db,
        layer=contours_layer,
        org=leshoz,
        started_by=gis_user,
        fmt="gpkg",
        content_type="application/geopackage+sqlite3",
        data=geopackage_bytes(
            tmp_path,
            [
                (
                    "POLYGON((400000 4600000, 401000 4600000, 401000 4601000,"
                    " 400000 4601000, 400000 4600000))",
                    {"number": "utm-1"},
                )
            ],
            crs="EPSG:32642",
        ),
    )


# --- Task 8: batch publication + the read API for 3.7/3.9 --------------------
#
# `approved_import`/`approved_import_with_one_overlap` are built directly at ORM
# level, already at the state `/publish` alone needs — mirrors task 5's
# `contour_with_two_versions`: what those two tests need is the STATE, not a
# second proof that submit-review/approve themselves work (already covered by
# `test_a_batch_is_reviewed_approved_and_published_in_three_calls`, which drives
# `processed_import` through all three calls for real). Every one of these gets
# combined with a `_client_for`-based client in its own test and therefore
# commits for real (the same lesson task 5/6/7's own fixtures document), so
# geometry is anchored with `random_box_wkt()`/`random_anchor()`, never the
# module's conventional box_wkt(69.9, 41.5) spot or its neighbours.


@pytest.fixture
async def processed_import(
    db: AsyncSession, session_factory: async_sessionmaker[AsyncSession], pending_import: GisImport
) -> GisImport:
    """Task 7's own terminal state for a clean batch — `status='review'`, two
    `draft` versions — reached by running the REAL job (never hand-built: what
    the job itself does right is task 7's own tests to prove, not this one's).
    """
    await import_service.process_pending(session_factory)
    await db.refresh(pending_import)
    return pending_import


@pytest.fixture
async def approved_import(
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    gis_user: User,
) -> GisImport:
    """A batch already through submit-review AND approve — two clean `approved`
    versions, ready for `/publish` alone. `file_id` reuses `approval_doc`'s own
    row (a real media_files id is all the FK needs; nothing in these tests
    ever reads it back)."""
    row = GisImport(
        layer_id=contours_layer.id,
        organization_id=leshoz.id,
        file_id=approval_doc.id,
        approval_doc_id=approval_doc.id,
        format="geojson",
        status="approved",
        stats={"created": 2, "warnings": []},
        started_by=gis_user.id,
        approved_by=gis_user.id,
    )
    db.add(row)
    await db.flush()
    for _ in range(2):
        contour = await make_contour(db, contours_layer, leshoz)
        await make_version(
            db,
            contour.id,
            random_box_wkt(),
            status="approved",
            approval_doc_id=approval_doc.id,
            import_id=row.id,
        )
    return row


@pytest.fixture
async def approved_import_with_one_overlap(
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    gis_user: User,
) -> GisImport:
    """A two-version batch, both `approved`: one clean, one overlapping an
    UNRELATED, already-published contour — ruling 1's own scenario, proving a
    single bad polygon does not hold its sibling hostage. The published contour
    and the overlapping version share one random anchor plus a fixed
    0.005-degree offset, the same half-step `approved_version_overlapping_a_
    published_one` (task 5) uses against `neighbouring_published_contour` — so
    the only thing the overlapping version ever overlaps is the row this
    fixture creates alongside it."""
    lon, lat = random_anchor()
    published = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db,
        published.id,
        box_wkt(lon, lat),
        status="published",
        approval_doc_id=approval_doc.id,
        published_at=func.now(),
    )
    row = GisImport(
        layer_id=contours_layer.id,
        organization_id=leshoz.id,
        file_id=approval_doc.id,
        approval_doc_id=approval_doc.id,
        format="geojson",
        status="approved",
        stats={"created": 2, "warnings": []},
        started_by=gis_user.id,
        approved_by=gis_user.id,
    )
    db.add(row)
    await db.flush()
    clean = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db,
        clean.id,
        random_box_wkt(),
        status="approved",
        approval_doc_id=approval_doc.id,
        import_id=row.id,
    )
    overlapping = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db,
        overlapping.id,
        box_wkt(lon + 0.005, lat),
        status="approved",
        approval_doc_id=approval_doc.id,
        import_id=row.id,
    )
    return row


@pytest.fixture
async def draft_contour(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization
) -> ContourVersion:
    """A contour with only a `draft` version, never published — the negative
    case `test_an_applicant_sees_published_contours_only` needs: `list_contours`
    joins to a PUBLISHED version (decision 6), so this one must never appear in
    its result. A dedicated fixture rather than reusing `draft_version`: that
    one is explicitly documented to never be combined with a `_client_for`
    client, and this test does exactly that (`applicant_client`)."""
    contour = await make_contour(db, contours_layer, leshoz)
    return await make_version(db, contour.id, random_box_wkt())


@pytest.fixture
async def published_fire_ban(db: AsyncSession) -> LayerFeature:
    """A published `fire_bans` feature (is_public=True, migration 0010) inside
    the fixed bbox `test_features_are_returned_as_a_geojson_feature_collection`
    queries (69.8,41.4 – 70.0,41.6) — the module's conventional box_wkt(69.9,
    41.5) spot already sits inside it, so this reuses that literal rather than
    inventing a second one the test's own bbox would have to match by hand."""
    layer = await repo.layer_by_code(db, "fire_bans")
    assert layer is not None
    today = business_today()
    return await make_feature(
        db,
        layer,
        box_wkt(69.9, 41.5),
        valid_from=today - timedelta(days=1),
        valid_to=today + timedelta(days=1),
    )


@pytest.fixture
async def published_restriction(db: AsyncSession) -> LayerFeature:
    """A published `restrictions` feature — not public (migration 0010), the
    layer `test_a_non_public_layer_is_refused_to_an_applicant` needs. That test
    never filters by bbox, so a random, isolated location is fine."""
    layer = await repo.layer_by_code(db, "restrictions")
    assert layer is not None
    return await make_feature(db, layer, random_box_wkt())


DRAIN_LIMIT = 50


@pytest.fixture(autouse=True)
async def _drain_leftover_imports(session_factory):
    """Empty the pending-import queue before EVERY test in this package.

    Autouse and in the package conftest, not in one test module: the claim is
    queue-WIDE (`process_pending` takes the oldest pending row in the database),
    and every import fixture commits, so any test module that files an import
    leaves rows for whatever runs next. When this lived in `test_import_job.py`
    alone it only worked because that module sorts first and happened to drain
    `test_imports_api.py`'s leftovers from the PREVIOUS run — so running
    `test_imports_api.py` on its own repeatedly would pile rows up until
    DRAIN_LIMIT tripped, and the next full run would fail. Task 8 adds more
    import tests, which would inherit exactly that order-dependence.

    Draining is what a real worker does anyway: no unscoped UPDATE/DELETE (which
    the lessons file forbids against this shared, persistent database), just the
    job running its course over whatever is queued.
    """
    await drain_pending_imports(session_factory)


async def drain_pending_imports(factory: async_sessionmaker[AsyncSession]) -> int:
    """Run the import job until the queue is empty, and say how many rows it
    took.

    The claim is queue-WIDE: `process_pending` takes the oldest `pending` row in
    the database, not the one a given test created. The import fixtures commit
    (the job runs in a session of its own and cannot see an uncommitted row), so
    an interrupted run strands a `pending` row in the shared, persistent test DB
    forever — and every later run would then claim that stranger instead of its
    own. Draining first is exactly what a real worker does: no unscoped
    UPDATE/DELETE, just the job running its course over whatever is queued.
    """
    for drained in range(DRAIN_LIMIT):
        if not await import_service.process_pending(factory):
            return drained
    raise AssertionError("the pending gis_imports queue would not drain")
