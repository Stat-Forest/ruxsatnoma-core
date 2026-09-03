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

import secrets
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.models import MediaFile
from app.core.time import business_today
from app.db import make_session_factory, uuid7
from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.applications.permissions import APPLICATIONS_REVIEW
from app.modules.auth.models import Applicant, Representation, User
from app.modules.gis.models import Contour, GisLayer
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.gis.conftest import (
    _client_for,
    _commit_pending_before_requests,
    box_wkt,
    make_contour,
    make_version,
    random_anchor,
    random_box_wkt,
)
from tests.modules.gis.conftest import approval_doc as approval_doc
from tests.modules.gis.conftest import contours_layer as contours_layer
from tests.modules.gis.conftest import gis_user as gis_user
from tests.modules.gis.conftest import leshoz as leshoz
from tests.modules.gis.conftest import other_leshoz as other_leshoz

# Task 4's pre-check prices the draft, so this package needs norms' own two
# fixtures. `published_coef_sb` is NOT optional and its absence looks like a bug
# in the pre-check: the ten seeded `coef_sb:*` rule parameters ship as DRAFTS
# (VMQ 689 annex 5 has not arrived) and the engine reads published rows only, so
# without it a grazing calculation cannot be computed AT ALL and
# `norms.service.preview` raises `ERR-NORM-004` naming the missing parameter.
# Both open their OWN session on purpose — see their docstrings.
from tests.modules.norms.conftest import published_coef_sb as published_coef_sb  # noqa: F401
from tests.modules.norms.conftest import (
    published_grazing_norm as published_grazing_norm,  # noqa: F401
)


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

    ONE permission, and it is exactly what migration 0015 grants
    `executor_staff`: `applications.review`. `applications.view_any` goes to
    `prosecutor` alone, so a fixture holding it would prove that a role nobody
    has can read an application (lesson: "A `_client_for` fixture's permission
    list must mirror the PRODUCTION role's grants" — a `_client_for` user
    inherits nothing from the real role's `role_permissions` row, whatever the
    fixture is named). What makes a real hodim able to READ what they review is
    `service._holds_staff_read`, which accepts `applications.review` or
    `.decide` or `.view_any` — this fixture is what proves that.
    """
    async for client in _client_for(db, APPLICATIONS_REVIEW, organization_id=leshoz.id):
        yield client


@pytest.fixture
async def other_zone_hodim_client(db: AsyncSession, other_leshoz: Organization):
    """The same reviewer shape, zoned to a DIFFERENT leshoz — the actor every
    territorial refusal is proven against. The same single grant as
    `hodim_client` on purpose: what differs between the two is the zone and
    nothing else, so a test that passes for one and fails for the other can
    only be about territory."""
    async for client in _client_for(db, APPLICATIONS_REVIEW, organization_id=other_leshoz.id):
        yield client


def unique_stir() -> str:
    """A fresh, valid-shape (`^[0-9]{9}$`) STIR per call — `applicants.stir` is
    UNIQUE and this test DB is shared and persistent. ASCII digits written out,
    never `\\d`: the column's CHECK is ASCII-only while Python's `\\d` is not
    (lesson)."""
    return f"{secrets.randbelow(10**9):09d}"


@pytest.fixture
async def legal_applicant(db: AsyncSession) -> Applicant:
    """A legal entity — `kind='legal'`, a STIR and NO `owner_user_id`: decision
    #9 gives a legal applicant no account of its own, so every application for
    it is filed by a representative."""
    row = Applicant(kind="legal", stir=unique_stir(), name="ООО Тест")
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def representative_client(db: AsyncSession, legal_applicant: Applicant):
    """A user who is themselves a registered individual applicant — required by
    `get_current_user`'s `ERR-AUTH-008` gate on any `applicant`-role account
    with no `Applicant` row of its own — AND holds an ACTIVE `Representation`
    over `legal_applicant`.

    The `Representation` row is built directly, the same way
    `tests/modules/payments/test_intents.py::representative_client` builds its
    own: the production path (`auth.service.attach_legal` /
    `add_representation`) needs a verified organisation ERI challenge and an
    existing director-or-org_eri representation to bootstrap from, none of
    which this module's rules depend on. `basis='org_eri'` needs no
    `poa_file_id`/`valid_until` — the DB CHECK requires those for
    `basis='poa'` only.
    """
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    db.add(
        Applicant(kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id)
    )
    db.add(
        Representation(
            applicant_id=legal_applicant.id,
            user_id=user.id,
            basis="org_eri",
            valid_from=business_today(),
        )
    )
    await db.flush()
    async for client in _client_for_applicant(db, user):
        yield client


async def _ready_draft(
    applicant_client,
    contour_id: uuid.UUID,
    activity_type_id: uuid.UUID,
    livestock_type_id: uuid.UUID,
    *,
    period_from: str,
    period_to: str,
) -> str:
    """One complete grazing draft, built through the REAL routes (`POST
    /applications` + `PATCH`), never by inserting an `Application` row (lesson:
    build a fixture's precondition through the real transition).

    Shared by the three draft fixtures below so that "complete" means the same
    thing in all of them: a second hand-written body is how two fixtures that
    are supposed to differ only in their period end up differing in more.
    """
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    assert created.status_code == 201, created.text
    application_id = created.json()["id"]
    patched = await applicant_client.patch(
        f"/api/v1/applications/{application_id}",
        json={
            "contour_id": str(contour_id),
            "activity_type_id": str(activity_type_id),
            "period_from": period_from,
            "period_to": period_to,
            "items": [{"livestock_type_id": str(livestock_type_id), "head_count": 40}],
        },
    )
    assert patched.status_code == 200, patched.text
    return application_id


@pytest.fixture
async def draft_ready_for_submission(
    applicant_client,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    sheep_type_id: uuid.UUID,
    published_coef_sb: None,
    published_grazing_norm: uuid.UUID,
) -> str:
    """A DRAFT carrying everything a submission needs: the contour, grazing, a
    May-September 2027 period and a 40-head sheep herd.

    Built through the REAL routes (`POST /applications` + `PATCH`), never by
    inserting an `Application` row (lesson: build a fixture's precondition
    through the real transition) — a draft assembled by hand would not prove
    that the shape task 5 refuses to submit is the shape task 3 lets an
    applicant reach.

    40 head is deliberately well inside `published_grazing_norm`'s MaxSB of 250,
    so a test that wants an over-limit herd raises it itself and a test that
    does not gets a clean pass.

    Returns the id as a STRING: every consumer interpolates it into a URL, and
    task 5's own tests re-parse it with `uuid.UUID(...)`.
    """
    return await _ready_draft(
        applicant_client,
        published_contour.id,
        grazing_activity_id,
        sheep_type_id,
        period_from="2027-05-01",
        period_to="2027-09-30",
    )


@pytest.fixture
async def second_draft_same_contour(
    applicant_client,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    sheep_type_id: uuid.UUID,
    published_coef_sb: None,
    published_grazing_norm: uuid.UUID,
) -> str:
    """The SAME applicant, contour and activity as `draft_ready_for_submission`,
    over a period that OVERLAPS its 2027-05-01..2027-09-30 — the four columns
    `ex_applications_no_duplicate` keys on (ruling 6, `tz/05` invariant 1).

    Submitting this one after the first must be refused by the DATABASE, never
    by a pre-SELECT: a "check then insert" is a race that lets two clicks a
    millisecond apart both succeed.
    """
    return await _ready_draft(
        applicant_client,
        published_contour.id,
        grazing_activity_id,
        sheep_type_id,
        period_from="2027-06-01",
        period_to="2027-08-31",
    )


@pytest.fixture
async def another_ready_draft(
    applicant_client,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    sheep_type_id: uuid.UUID,
    published_coef_sb: None,
    published_grazing_norm: uuid.UUID,
) -> str:
    """A second submittable draft for the same applicant that CANNOT collide
    with `draft_ready_for_submission`: the same contour and activity, a season a
    year later, so `daterange(period_from, period_to, '[]') &&` is false and the
    EXCLUDE constraint has nothing to say about the pair.

    Deliberately not a second contour: `published_grazing_norm` is defined for
    `published_contour` alone, and a draft on a contour with no published norm
    would be refused `ERR-NORM-001` before it ever reached the number allocator
    this fixture exists to observe.
    """
    return await _ready_draft(
        applicant_client,
        published_contour.id,
        grazing_activity_id,
        sheep_type_id,
        period_from="2028-05-01",
        period_to="2028-09-30",
    )


@pytest.fixture
async def doc_type_item_id(engine) -> AsyncIterator[uuid.UUID]:
    """One `doc_types` classifier item — migration 0005 seeds the CLASSIFIER but
    none of its items, so `application_documents.doc_type_item_id` has nothing to
    point at until a test makes one.

    Its own session and its own teardown, the `tests/modules/norms/conftest.py::
    benefit_category` pattern: a client commits `db` before every request
    (lesson), so a row added through the test's own session would survive that
    session's rollback and accumulate in the shared, persistent test database.
    The code carries a random suffix so two runs can never collide on
    `uq_classifier_items_active_code`."""
    item_id = uuid7()
    factory = make_session_factory(engine)
    async with factory() as own_db:
        await own_db.execute(
            text(
                "INSERT INTO classifier_items "
                "(id, classifier_id, code, name, valid_from, sort_order, status) "
                "SELECT :id, c.id, :code, CAST(:name AS jsonb), DATE '2020-01-01', 0, 'active' "
                "FROM classifiers c WHERE c.code = 'doc_types'"
            ).bindparams(
                id=item_id,
                code=f"benefit_proof_{uuid.uuid4().hex[:8]}",
                name='{"en": "Benefit proof (test)"}',
            )
        )
        await own_db.commit()
        try:
            yield item_id
        finally:
            # The attachments first: an HTTP-driven test COMMITS its
            # `application_documents` rows (the app's own session, not the test's
            # `db`), so they outlive the test and hold an FK on this item.
            await own_db.execute(
                text("DELETE FROM application_documents WHERE doc_type_item_id = :id").bindparams(
                    id=item_id
                )
            )
            await own_db.execute(
                text("DELETE FROM classifier_items WHERE id = :id").bindparams(id=item_id)
            )
            await own_db.commit()


@pytest.fixture
async def benefit_doc_type_item_id(engine) -> AsyncIterator[uuid.UUID]:
    """The `doc_types` item whose code is EXACTLY
    `service.BENEFIT_DOC_TYPE_CODE` — the one document type a benefit claim can
    be proven with (ruling 10а, fail-closed).

    Unlike `doc_type_item_id` above, the code cannot carry a random suffix: the
    submission looks the item up BY that code. `uq_classifier_items_active_code`
    therefore makes it a shared name in a shared, persistent database, so this
    reuses an existing active row when one is there and only deletes what it
    inserted itself — a blanket delete would strip a row another run is using
    (lesson: the test DB is shared, persistent and never empty).
    """
    from app.modules.applications.service import BENEFIT_DOC_TYPE_CODE

    factory = make_session_factory(engine)
    async with factory() as own_db:
        existing = await own_db.scalar(
            text(
                "SELECT i.id FROM classifier_items i JOIN classifiers c ON c.id = i.classifier_id "
                "WHERE c.code = 'doc_types' AND i.code = :code AND i.status = 'active'"
            ).bindparams(code=BENEFIT_DOC_TYPE_CODE)
        )
        if existing is not None:
            yield existing
            return
        item_id = uuid7()
        await own_db.execute(
            text(
                "INSERT INTO classifier_items "
                "(id, classifier_id, code, name, valid_from, sort_order, status) "
                "SELECT :id, c.id, :code, CAST(:name AS jsonb), DATE '2020-01-01', 0, 'active' "
                "FROM classifiers c WHERE c.code = 'doc_types'"
            ).bindparams(id=item_id, code=BENEFIT_DOC_TYPE_CODE, name='{"en": "Benefit proof"}')
        )
        await own_db.commit()
        try:
            yield item_id
        finally:
            # The attachments first: an HTTP-driven test COMMITS its
            # `application_documents` rows, so they outlive it and hold an FK.
            await own_db.execute(
                text("DELETE FROM application_documents WHERE doc_type_item_id = :id").bindparams(
                    id=item_id
                )
            )
            await own_db.execute(
                text("DELETE FROM classifier_items WHERE id = :id").bindparams(id=item_id)
            )
            await own_db.commit()


@pytest.fixture
async def benefit_category_item_id(engine) -> AsyncIterator[uuid.UUID]:
    """One `benefit_categories` classifier item, by ID.

    `tests/modules/norms/conftest.py::benefit_category` yields the CODE, which
    is what `norms` speaks; `applications.benefit_category_item_id` is an FK to
    `classifier_items`, so this package needs the id. Same own-session +
    teardown pattern as `doc_type_item_id` above and for the same reason: an
    HTTP-driven test commits, so a row added through the test's own session
    would survive that session's rollback and accumulate in the shared,
    persistent test database.

    VMQ 278's real benefit list has not arrived (`tz/12` #2), so the seeded
    classifier is empty and nothing can claim a benefit without this.
    """
    item_id = uuid7()
    factory = make_session_factory(engine)
    async with factory() as own_db:
        await own_db.execute(
            text(
                "INSERT INTO classifier_items "
                "(id, classifier_id, code, name, valid_from, sort_order, status) "
                "SELECT :id, c.id, :code, CAST(:name AS jsonb), DATE '2020-01-01', 0, 'active' "
                "FROM classifiers c WHERE c.code = 'benefit_categories'"
            ).bindparams(
                id=item_id,
                code=f"veteran_{uuid.uuid4().hex[:8]}",
                name='{"en": "Veteran (test)"}',
            )
        )
        await own_db.commit()
        try:
            yield item_id
        finally:
            await own_db.execute(
                text(
                    "UPDATE applications SET benefit_category_item_id = NULL "
                    "WHERE benefit_category_item_id = :id"
                ).bindparams(id=item_id)
            )
            await own_db.execute(
                text("DELETE FROM classifier_items WHERE id = :id").bindparams(id=item_id)
            )
            await own_db.commit()


@pytest.fixture
async def science_activity_id(db: AsyncSession) -> uuid.UUID:
    """`science` — «Илмий тадқиқот», the one activity VMQ 278 leaves un-rated
    (`tariff_exempt:science`, published by migration 0013). Seeded by 0005,
    never created here: `activity_types` is a fixed catalogue."""
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'science'"))
    return rows.scalar_one()


@pytest.fixture
async def overlapping_published_contour(
    db: AsyncSession, contours_layer: GisLayer, leshoz, approval_doc: MediaFile
) -> Contour:
    """TWO published contours that genuinely overlap, of which this returns the
    one an application may name.

    Both boxes are anchored off ONE `random_anchor()` and offset by half a box,
    so the overlap is a fact about this pair rather than about whatever else has
    accumulated at the module's conventional `box_wkt(69.9, 41.5)` spot across
    past runs (lesson: the test DB is shared, persistent, and never empty —
    including the spot you picked). `random_box_wkt()` cannot build this: it
    picks a fresh anchor per call, so two of them never meet.

    What it buys: `gis_overlap` comes back `fail` with real `details.items`,
    each carrying a raw `uuid.UUID` (`feature_id`) and a raw `Decimal`
    (`area_m2`) straight out of `gis.checks._intersections` — the only path that
    exercises `checks._jsonable` on values nothing else has coerced.
    """
    lon, lat = random_anchor()
    neighbour = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db,
        neighbour.id,
        box_wkt(lon, lat),
        status="published",
        approval_doc_id=approval_doc.id,
    )
    contour = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db,
        contour.id,
        box_wkt(lon + 0.005, lat),
        status="published",
        approval_doc_id=approval_doc.id,
    )
    await db.flush()
    return contour
