"""`/api/v1/public/*` (decision #63) — the anonymous door a citizen with no
session and no parcel reaches from the public `landing` site. `client` here
carries no session cookie at all, the same idiom
`tests/modules/permits/conftest.py::client` uses for С12's own anonymous
surface — proving these routes need no login is the whole point, so a
`_client_for(...)`-built role client would prove nothing.

Nothing here touches `calc_router.py`/`service.preview`/`CalculationIn` —
`test_preview_api.py` and `test_calculations_api.py` are the proof that the
authenticated surface is unchanged, run unmodified alongside this file."""

import uuid
from collections.abc import AsyncIterator
from datetime import date

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from tests.conftest import make_client
from tests.modules.gis.conftest import _commit_pending_before_requests

pytestmark = pytest.mark.asyncio

ESTIMATE = "/api/v1/public/calculations/estimate"
ACTIVITY_TYPES = "/api/v1/public/refs/activity-types"
LIVESTOCK_TYPES = "/api/v1/public/refs/livestock-types"

ESTIMATE_FIELDS = {
    "approximate",
    "disclaimer",
    "checks_skipped",
    "activity_type_id",
    "period_from",
    "period_to",
    "quantity",
    "items",
    "amount",
    "used_sb",
    "rule_code_version",
    "breakdown",
}


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """No session cookie at all — the whole contract under test is that these
    routes work without one."""
    async with make_client(create_app(), lifespan=True) as anonymous:
        _commit_pending_before_requests(anonymous, db)
        yield anonymous


async def test_a_haymaking_estimate_returns_a_number_and_says_it_is_approximate(
    client: httpx.AsyncClient,
    haymaking_activity_id: uuid.UUID,
    frozen_on_date: date,
) -> None:
    """Same arithmetic and the same `bhm` window
    `test_a_haymaking_preview_returns_the_amount_and_the_checks` pins (412 000),
    reached with no `contour_id` at all."""
    response = await client.post(
        ESTIMATE,
        json={
            "activity_type_id": str(haymaking_activity_id),
            "period_from": "2026-06-01",
            "period_to": "2026-09-30",
            "quantity": "4",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == ESTIMATE_FIELDS, "a field added here is published to the whole internet"
    assert body["amount"] == "2472000"  # 4 ha × 1.5 × 412 000, same as the authenticated preview
    assert body["approximate"] is True
    assert "approximate" in body["disclaimer"].lower() or "not a binding" in body["disclaimer"]
    assert set(body["checks_skipped"]) == {"norm", "season", "rotation", "fire_ban", "limit"}
    assert body["used_sb"] is None
    assert body["rule_code_version"] == "norms-1.0.0"


async def test_a_grazing_estimate_reports_the_missing_coefficient_rather_than_guessing(
    client: httpx.AsyncClient, grazing_activity_id: uuid.UUID
) -> None:
    """The ten `coef_sb:*` rows are drafts on a fresh database (ruling 8) —
    the same honest refusal `test_a_grazing_preview_...` pins for the
    authenticated route, here with no contour at all. Never a 500, never an
    invented number."""
    response = await client.post(
        ESTIMATE,
        json={
            "activity_type_id": str(grazing_activity_id),
            "period_from": "2026-05-01",
            "period_to": "2026-09-30",
            "items": [{"livestock_code": "sheep_goat_6m", "count": 50}],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR-NORM-004"
    assert response.json()["error"]["details"]["code"].startswith("coef_sb:")


async def test_an_estimate_writes_nothing(
    client: httpx.AsyncClient,
    db: AsyncSession,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """`calculations` is append-only and belongs to real applications — an
    anonymous estimate must never add a row to it."""
    before = (await db.execute(text("SELECT count(*) FROM calculations"))).scalar_one()
    await client.post(
        ESTIMATE,
        json={
            "activity_type_id": str(haymaking_activity_id),
            "period_from": "2026-06-01",
            "period_to": "2026-09-30",
            "quantity": "4",
        },
    )
    after = (await db.execute(text("SELECT count(*) FROM calculations"))).scalar_one()
    assert after == before


async def test_no_contour_or_application_or_benefit_can_be_smuggled_in(
    client: httpx.AsyncClient,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """`PublicEstimateIn` simply has no `contour_id`/`application_id`/
    `benefit_code` fields — extra keys in the body are dropped by pydantic,
    never read, never able to reach a benefit modifier or bind to a real
    application."""
    response = await client.post(
        ESTIMATE,
        json={
            "activity_type_id": str(haymaking_activity_id),
            "period_from": "2026-06-01",
            "period_to": "2026-09-30",
            "quantity": "4",
            "contour_id": str(uuid.uuid4()),
            "application_id": str(uuid.uuid4()),
            "benefit_code": "veteran",
        },
    )
    assert response.status_code == 200
    assert set(response.json()) == ESTIMATE_FIELDS


async def test_a_reversed_period_is_refused(
    client: httpx.AsyncClient, haymaking_activity_id: uuid.UUID
) -> None:
    """`checks.run_checks` is never called here (no contour to check season/
    rotation/fire-ban against — see `service.estimate_public`'s own block
    comment), so this guard is this function's own, reusing
    `checks.MAX_PERIOD_DAYS` rather than re-deriving it."""
    response = await client.post(
        ESTIMATE,
        json={
            "activity_type_id": str(haymaking_activity_id),
            "period_from": "2026-09-30",
            "period_to": "2026-06-01",
            "quantity": "4",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "period_reversed"


async def test_an_unknown_activity_type_is_refused_not_500(client: httpx.AsyncClient) -> None:
    response = await client.post(
        ESTIMATE,
        json={
            "activity_type_id": str(uuid.uuid4()),
            "period_from": "2026-06-01",
            "period_to": "2026-09-30",
            "quantity": "4",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "unknown_activity_type"


async def test_the_estimate_route_is_rate_limited(
    client: httpx.AsyncClient, haymaking_activity_id: uuid.UUID
) -> None:
    """Default `ratelimit_public_calc_estimate_per_minute` is 20 — an anonymous
    compute endpoint is the obvious abuse target."""
    payload = {
        "activity_type_id": str(haymaking_activity_id),
        "period_from": "2026-06-01",
        "period_to": "2026-09-30",
        "quantity": "4",
    }
    codes = set()
    for _ in range(30):
        codes.add((await client.post(ESTIMATE, json=payload)).status_code)
    assert 429 in codes
    assert 200 in codes


# --- the two narrow catalogs ---------------------------------------------------


async def test_public_activity_types_answers_anonymously_with_id_code_name(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(ACTIVITY_TYPES)
    assert response.status_code == 200
    items = response.json()
    assert len(items) >= 6  # the seeded catalog, migration 0005
    grazing = next(item for item in items if item["code"] == "grazing")
    assert set(grazing) == {"id", "code", "name"}
    assert set(grazing["name"]) <= {"en", "uz_cyrl"}, "no Latin-script Uzbek yet (tz/12 #31)"


async def test_public_livestock_types_answers_anonymously_with_id_code_name(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(LIVESTOCK_TYPES)
    assert response.status_code == 200
    items = response.json()
    assert len(items) >= 10  # tz/06's ten groups, migration 0005
    cattle = next(item for item in items if item["code"] == "cattle_adult")
    assert set(cattle) == {"id", "code", "name"}


async def test_the_refs_routes_are_rate_limited(client: httpx.AsyncClient) -> None:
    """Default `ratelimit_public_refs_per_minute` is 60."""
    codes = {(await client.get(ACTIVITY_TYPES)).status_code for _ in range(70)}
    assert 429 in codes
