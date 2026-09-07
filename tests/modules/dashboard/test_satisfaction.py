"""`GET /api/v1/dashboard/kpi`'s `satisfaction` block (Task 6, ruling #143):
the citizen's ratings, scoped and filtered exactly like every other KPI tile —
same `_combined(...)` three-axis zone clause `permits_kpi` uses, and the
period applies to `permit_ratings.created_at` (when the citizen rated), never
`permits.issued_at`.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest

from app.modules.dashboard.permissions import DASHBOARD_VIEW
from app.modules.permits.models import PermitRating
from tests.modules.gis.conftest import _client_for, make_contour, make_version, random_box_wkt
from tests.modules.gis.conftest import approval_doc as approval_doc  # noqa: F401
from tests.modules.gis.conftest import contours_layer as contours_layer  # noqa: F401
from tests.modules.permits.conftest import grazing_activity_id as grazing_activity_id  # noqa: F401
from tests.modules.permits.conftest import (
    haymaking_activity_id as haymaking_activity_id,  # noqa: F401
)
from tests.modules.permits.conftest import make_permit_on_contour

API = "/api/v1"

# `make_permit_on_contour`'s own default period, which is also `test_kpi.py`'s
# PERIOD_FROM/PERIOD_TO — 2027, far enough past this suite's real run date
# that no OTHER fixture's `now()`-defaulted `created_at` ever lands inside it
# by accident (this shared, persistent test DB never starts empty).
PERIOD_FROM = date(2027, 5, 1)
PERIOD_TO = date(2027, 9, 30)

# Inside the period above, held fixed so every rating's `created_at` is
# unambiguously in scope regardless of when the suite actually runs.
RATED_AT = datetime(2027, 6, 1, tzinfo=UTC)


@pytest.fixture
async def dashboard_client(db, leshoz):
    """`ratings.view`-shaped but for `dashboard`: a `DASHBOARD_VIEW` holder
    zoned to THIS test's own `leshoz`, the same shape `test_kpi.py`'s own
    `_client_for_zoned` builds."""
    async for client in _client_for(db, DASHBOARD_VIEW, organization_id=leshoz.id):
        yield client


@dataclass(frozen=True)
class SeededRatings:
    grazing_id: uuid.UUID
    haymaking_id: uuid.UUID


@pytest.fixture
async def seeded_ratings(
    db,
    leshoz,
    contours_layer,
    approval_doc,
    grazing_activity_id: uuid.UUID,
    haymaking_activity_id: uuid.UUID,
) -> SeededRatings:
    """Three ratings on THIS test's own `leshoz` — a fresh organization every
    run, so the zone-scoped assertions below cannot be satisfied by a row a
    different test left on this shared, persistent database (lesson). One
    grazing permit (score 5) and two haymaking permits (scores 3, 4): count 3,
    avg exactly (5+3+4)/3 = 4.00, and narrowing to `grazing_id` must answer
    count 1 — the shape the brief's own test needs.

    `created_at` is set explicitly to `RATED_AT`, not left to the column's
    `server_default=func.now()`: "now" during a real test run is nowhere near
    2027, so the period filter would find nothing at all.
    """
    contour = await make_contour(db, contours_layer, leshoz)
    version = await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    for activity_type_id, score in (
        (grazing_activity_id, 5),
        (haymaking_activity_id, 3),
        (haymaking_activity_id, 4),
    ):
        permit = await make_permit_on_contour(
            db,
            contour=contour,
            version_id=version.id,
            org=leshoz,
            activity_type_id=activity_type_id,
            status="active",
        )
        db.add(PermitRating(permit_id=permit.id, score=score, created_at=RATED_AT))
    await db.flush()
    return SeededRatings(grazing_id=grazing_activity_id, haymaking_id=haymaking_activity_id)


async def test_the_satisfaction_tile_obeys_the_dashboard_filters(
    dashboard_client, seeded_ratings: SeededRatings
) -> None:
    body = (
        await dashboard_client.get(
            f"{API}/dashboard/kpi",
            params={"period_from": PERIOD_FROM.isoformat(), "period_to": PERIOD_TO.isoformat()},
        )
    ).json()
    assert body["satisfaction"] == {"avg_score": "4.00", "count": 3}

    narrowed = (
        await dashboard_client.get(
            f"{API}/dashboard/kpi",
            params={
                "period_from": PERIOD_FROM.isoformat(),
                "period_to": PERIOD_TO.isoformat(),
                "activity_type_id": str(seeded_ratings.grazing_id),
            },
        )
    ).json()
    assert narrowed["satisfaction"]["count"] == 1


async def test_an_empty_period_says_none_rather_than_zero(dashboard_client) -> None:
    """A portal may not state a number it cannot produce (F7's own lesson): no
    ratings means no average, not an average of 0."""
    body = (
        await dashboard_client.get(
            f"{API}/dashboard/kpi",
            params={"period_from": "2020-01-01", "period_to": "2020-12-31"},
        )
    ).json()
    assert body["satisfaction"] == {"avg_score": None, "count": 0}


async def test_the_satisfaction_tile_is_zone_scoped(
    db, leshoz, other_leshoz, seeded_ratings: SeededRatings
) -> None:
    """`_combined(...)`'s own reason for existing (context this task shipped
    with): a DIFFERENT leshoz's `DASHBOARD_VIEW` holder must see none of
    `seeded_ratings`' three rows — proves the zone clause is actually wired
    into `satisfaction_kpi`, not only present in its signature."""
    async for other_client in _client_for(db, DASHBOARD_VIEW, organization_id=other_leshoz.id):
        body = (
            await other_client.get(
                f"{API}/dashboard/kpi",
                params={
                    "period_from": PERIOD_FROM.isoformat(),
                    "period_to": PERIOD_TO.isoformat(),
                },
            )
        ).json()
        assert body["satisfaction"] == {"avg_score": None, "count": 0}
