"""`GET /public/ratings/summary` — the landing's single national rating
number, suppressed below the k-anonymity threshold (#174). No `average`, no
`histogram` below the threshold: an average over a handful of ratings
published as a national figure is exactly the "hides or overstates" defect
this project keeps finding.

This route reports a NATIONAL, un-scoped total — no zone or period filter
narrows it the way `GET /admin/ratings/summary` and the dashboard's
`satisfaction` tile do (`permits.repo._ratings_conditions`), so unlike those
two it cannot lean on a fresh zone or a held-fixed 2027 period to keep this
shared, persistent test database's leftover rows out of its count.
`.claude/lessons.md`'s own rule ("The test DB is shared, persistent, and
never empty") forbids working around that with an unscoped `DELETE` or by
assuming the table starts empty, so the suppression CONTRACT below is proven
by monkeypatching `repo.rating_histogram` — the same idiom `tests/modules/
permits/test_ratings.py::test_a_double_clicked_rating_answers_409_not_500`
already uses to get a deterministic repo answer under a real, HTTP-driven
request — rather than by depending on the real table's exact population. A
separate, real-DB test at the bottom proves the wiring end to end with a
BEFORE/AFTER delta on `count`, never an absolute value: the lesson's own
remedy for a global aggregate ("assert on rows carrying your fixture's own
ids").

No `permit_rating_factory` exists anywhere in the suite (checked
`tests/conftest.py` and `tests/modules/permits/conftest.py`); the one below
is local to this file, built through `PermitRating` and
`make_permit_on_contour` the same way `tests/modules/permits/test_ratings.py`'s
own `seeded_ratings` fixture and `tests/modules/dashboard/test_satisfaction.py`'s
do — never through the citizen-facing `POST /permits/{id}/rating`, which
enforces "the holder, once" (ruling #140) and has nothing to do with the
threshold this task is about.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.gis.models import GisLayer
from app.modules.permits.models import PermitRating
from app.modules.public import repo
from tests.conftest import make_client
from tests.modules.gis.conftest import approval_doc as approval_doc  # noqa: F401
from tests.modules.gis.conftest import contours_layer as contours_layer  # noqa: F401
from tests.modules.gis.conftest import leshoz as leshoz  # noqa: F401
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt
from tests.modules.permits.conftest import grazing_activity_id as grazing_activity_id  # noqa: F401
from tests.modules.permits.conftest import make_permit_on_contour

API = "/api/v1"


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    """Same guard every module under `tests/modules/` carries (lesson):
    without it `create_app()` opens the shared DEV database instead of the
    test one."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _stub_histogram(histogram: dict[int, int]):
    """A `repo.rating_histogram` replacement that ignores `db` and answers a
    fixed shape — deterministic regardless of what this shared database
    already holds."""

    async def stub(db: AsyncSession) -> dict[int, int]:
        return dict(histogram)

    return stub


# --- The suppression contract, deterministic regardless of the shared table -


async def test_thin_data_is_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo, "rating_histogram", _stub_histogram({5: 1, 4: 1}))  # count 2

    async with make_client(create_app(), lifespan=True) as client:
        body = (await client.get(f"{API}/public/ratings/summary")).json()

    assert body["published"] is False
    assert body["average"] is None
    assert body["histogram"] is None
    assert body["count"] == 2
    assert body["threshold"] == 5


async def test_the_average_appears_once_the_threshold_is_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repo, "rating_histogram", _stub_histogram({5: 2, 4: 2, 3: 1}))  # count 5

    async with make_client(create_app(), lifespan=True) as client:
        body = (await client.get(f"{API}/public/ratings/summary")).json()

    assert body["published"] is True
    assert body["count"] == 5
    assert body["average"] == "4.2"
    assert body["histogram"] == {"1": 0, "2": 0, "3": 1, "4": 2, "5": 2}


async def test_no_ratings_at_all_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo, "rating_histogram", _stub_histogram({}))

    async with make_client(create_app(), lifespan=True) as client:
        body = (await client.get(f"{API}/public/ratings/summary")).json()

    assert body["published"] is False
    assert body["average"] is None
    assert body["histogram"] is None
    assert body["count"] == 0


# --- The real wiring, against the real (shared) table, via a delta ----------


@pytest.fixture
async def permit_rating_factory(
    db: AsyncSession,
    leshoz: Organization,
    contours_layer: GisLayer,
    approval_doc,
    grazing_activity_id,
):
    """Returns an async `make(score)` that creates a fresh `active` permit and
    rates it once. One contour and one published version, shared across every
    call within a test — `make_permit_on_contour`'s own docstring: several
    permits on one contour is exactly what it is for, and no assertion here
    needs a fresh contour per rating."""
    contour = await make_contour(db, contours_layer, leshoz)
    version = await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )

    async def make(score: int) -> PermitRating:
        permit = await make_permit_on_contour(
            db,
            contour=contour,
            version_id=version.id,
            org=leshoz,
            activity_type_id=grazing_activity_id,
            status="active",
        )
        rating = PermitRating(permit_id=permit.id, score=score)
        db.add(rating)
        await db.flush()
        return rating

    return make


async def test_the_route_reflects_real_ratings_end_to_end(
    db: AsyncSession, permit_rating_factory
) -> None:
    """No mock on this one — the real repo query against the real,
    already-populated table, proven by a BEFORE/AFTER delta on `count` rather
    than an absolute value (`.claude/lessons.md`: this shared test database is
    never empty, and this route has no zone or period filter to narrow around
    that with)."""
    async with make_client(create_app(), lifespan=True) as client:
        before = (await client.get(f"{API}/public/ratings/summary")).json()["count"]

    for score in (5, 4, 3):
        await permit_rating_factory(score)
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        after = (await client.get(f"{API}/public/ratings/summary")).json()["count"]

    assert after - before == 3
