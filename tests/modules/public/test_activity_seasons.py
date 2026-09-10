"""`GET /public/activity-seasons` — the REAL season windows (stage 8 fix wave
finding 1, supersedes the R3 half of decision #175), replacing the deleted
`site_season_windows` settings key.

The anonymous read names no leshoz, so it resolves the AGENCY's own
`activity_seasons` rows — the nationwide default — through
`norms.service.effective_season`, the same path the wizard's date picker and
`norms.checks._season_check` use. This shared database may hold an agency
row for any activity another test left behind, so every assertion here is
made against the rows that ACTUALLY exist at the time, never against an
absolute "nothing is configured" the database does not promise."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import ActivityType, Organization
from app.modules.norms import service as norms_service
from app.modules.norms.models import ActivitySeason
from app.modules.public import service
from tests.conftest import make_client
from tests.modules.auth.test_sessions import make_user

API = "/api/v1"

SEEDED_ACTIVITY_CODES = {"grazing", "haymaking", "apiary", "recreation", "deadwood", "science"}


@pytest.fixture
async def agency(db: AsyncSession) -> Organization:
    """Committed get-or-create, the way `tests/modules/admin/conftest.py`'s
    fixture of the same name does it: ruling 6 allows exactly one root row
    and the partial unique index would refuse a second."""
    existing = await admin_repo.get_agency(db)
    if existing is not None:
        return existing
    org = Organization(
        kind="agency",
        code="agency",
        name={"uz_latn": "Oʻrmon xoʻjaligi agentligi", "en": "Forestry Agency"},
    )
    db.add(org)
    await db.commit()
    return org


async def _agency_rows(db: AsyncSession, agency: Organization) -> dict[str, ActivitySeason]:
    rows = (
        await db.execute(
            select(ActivitySeason, ActivityType.code)
            .join(ActivityType, ActivityType.id == ActivitySeason.activity_type_id)
            .where(ActivitySeason.organization_id == agency.id)
        )
    ).all()
    return {code: row for row, code in rows}


async def test_route_is_reachable_anonymously_and_covers_the_catalogue(db) -> None:
    async with make_client(create_app(), lifespan=True) as client:
        response = await client.get(f"{API}/public/activity-seasons")
    assert response.status_code == 200
    body = response.json()
    codes = {row["activity_type_code"] for row in body}
    assert SEEDED_ACTIVITY_CODES <= codes


async def test_an_activity_without_an_agency_row_is_none_not_open_all_year(db, agency) -> None:
    """No Agency row for an activity means nobody has recorded a season
    anywhere the public read can see — `windows: []`, `season_source:
    "none"`, never a manufactured "always in season"."""
    configured = await _agency_rows(db, agency)
    rows = await service.public_activity_seasons(db)
    unconfigured = [row for row in rows if row.activity_type_code not in configured]
    assert unconfigured, "every seeded activity has an Agency row — nothing left to assert on"
    for row in unconfigured:
        assert row.windows == [], row.activity_type_code
        assert row.season_source == "none", row.activity_type_code
        assert row.is_default is True, row.activity_type_code


async def test_the_agency_row_is_the_public_calendar(db, agency) -> None:
    """The one behaviour this file exists for: an `activity_seasons` row on
    the ROOT organization is what the anonymous read shows. Flushed, never
    committed — the `db` fixture's rollback removes it again, so the shared
    database is left as it was found."""
    configured = await _agency_rows(db, agency)
    activity = next(a for a in await admin_repo.list_activity_types(db) if a.code not in configured)
    author = await make_user(db, role_code="sys_admin")
    windows = [{"from": "05-01", "to": "09-30"}, {"from": "11-01", "to": "11-15"}]
    db.add(
        ActivitySeason(
            organization_id=agency.id,
            activity_type_id=activity.id,
            season={"windows": windows},
            created_by=author.id,
        )
    )
    await db.flush()

    rows = {row.activity_type_code: row for row in await service.public_activity_seasons(db)}
    assert rows[activity.code].windows == windows
    assert rows[activity.code].season_source == "activity_season"
    assert rows[activity.code].is_default is True


async def test_agrees_with_effective_season_for_the_agency(db, agency) -> None:
    """The one thing this route must never do: re-derive the precedence
    itself. Every row must equal a direct call to `norms.service.
    effective_season` for the Agency — the SAME function the wizard's date
    picker reads, resolving through the SAME `resolve_effective_windows`
    the submit check calls."""
    rows = await service.public_activity_seasons(db)
    assert rows, "the seeded catalogue must not be empty"
    by_code = {a.code: a.id for a in await admin_repo.list_activity_types(db)}
    for row in rows:
        expected = await norms_service.effective_season(
            db,
            activity_type_id=by_code[row.activity_type_code],
            contour_id=None,
            organization_id=agency.id,
        )
        assert row.windows == expected["windows"], row.activity_type_code
        assert row.season_source == expected["season_source"], row.activity_type_code


async def test_the_response_carries_no_leshoz_specific_data(db) -> None:
    async with make_client(create_app(), lifespan=True) as client:
        body = (await client.get(f"{API}/public/activity-seasons")).json()
    assert body
    for row in body:
        assert set(row) == {"activity_type_code", "windows", "season_source", "is_default"}


async def test_a_leshoz_row_alone_does_not_reach_the_public_calendar(db, agency) -> None:
    """A leshoz's own dictionary row is that leshoz's business (ruling #177):
    the anonymous read shows the Agency's default only, so a season one
    forestry set for itself must not be presented as the country's."""
    configured = await _agency_rows(db, agency)
    activity = next(a for a in await admin_repo.list_activity_types(db) if a.code not in configured)
    author = await make_user(db, role_code="sys_admin")
    leshoz = Organization(
        kind="leshoz",
        code=f"seasons-public-{uuid.uuid4().hex[:8]}",
        name={"uz_latn": "Sinov DOʻX"},
        parent_id=agency.id,
    )
    db.add(leshoz)
    await db.flush()
    db.add(
        ActivitySeason(
            organization_id=leshoz.id,
            activity_type_id=activity.id,
            season={"windows": [{"from": "01-01", "to": "12-31"}]},
            created_by=author.id,
        )
    )
    await db.flush()

    rows = {row.activity_type_code: row for row in await service.public_activity_seasons(db)}
    assert rows[activity.code].windows == []
    assert rows[activity.code].season_source == "none"
