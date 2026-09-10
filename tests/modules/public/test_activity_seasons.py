"""`GET /public/activity-seasons` — the REAL season windows (stage 8 fix wave
finding 1, supersedes the R3 half of decision #175), replacing the deleted
`site_season_windows` settings key.

The route calls `norms.checks.resolve_effective_windows` with no leshoz
context (`(None, None)`) for every activity in the catalogue, so its answer
is deterministic regardless of what `activity_seasons`/`norms` rows other
tests in this shared database happen to hold — nothing here needs a fresh
organization or contour to prove that."""

from app.main import create_app
from app.modules.norms import service as norms_service
from app.modules.public import service
from tests.conftest import make_client

API = "/api/v1"

SEEDED_ACTIVITY_CODES = {"grazing", "haymaking", "apiary", "recreation", "deadwood", "science"}


async def test_route_is_reachable_anonymously_and_covers_the_catalogue(db) -> None:
    async with make_client(create_app(), lifespan=True) as client:
        response = await client.get(f"{API}/public/activity-seasons")
    assert response.status_code == 200
    body = response.json()
    codes = {row["activity_type_code"] for row in body}
    assert SEEDED_ACTIVITY_CODES <= codes


async def test_no_configured_season_is_reported_as_none_not_open_all_year(db) -> None:
    """With no leshoz named there is no `activity_seasons` dictionary row to
    fall back to and no contour whose norm could override it — every
    activity must answer "nothing configured" (`windows: []`,
    `season_source: "none"`), never a manufactured "always in season"."""
    rows = await service.public_activity_seasons(db)
    for row in rows:
        assert row.windows == [], row.activity_type_code
        assert row.season_source == "none", row.activity_type_code
        assert row.is_default is True, row.activity_type_code


async def test_agrees_with_resolve_effective_windows_for_the_same_inputs(db) -> None:
    """The one thing this route must never do: re-derive the precedence
    itself. Every row must equal a direct call to the SAME function
    `norms.checks._season_check` calls, for the SAME (no-leshoz) inputs."""
    expected_windows, expected_source = norms_service.resolve_effective_windows(None, None)
    rows = await service.public_activity_seasons(db)
    assert rows, "the seeded catalogue must not be empty"
    for row in rows:
        assert row.windows == expected_windows
        assert row.season_source == expected_source


async def test_the_response_carries_no_leshoz_specific_data(db) -> None:
    async with make_client(create_app(), lifespan=True) as client:
        body = (await client.get(f"{API}/public/activity-seasons")).json()
    assert body
    for row in body:
        assert set(row) == {"activity_type_code", "windows", "season_source", "is_default"}
