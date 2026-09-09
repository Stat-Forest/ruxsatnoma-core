"""Ruling #177 task 4 — `GET /activity-seasons/effective`, the public read
the wizard (T10, wave 3) constrains its date pickers from. Resolves through
`checks.resolve_effective_windows`, the exact function the blocking check
itself calls (`test_checks.py`'s own unit test on that function), so this
file only has to prove the HTTP surface names the right fields and passes
the right arguments through — not re-derive the precedence."""

import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization
from app.modules.auth.models import User
from app.modules.gis.models import Contour
from app.modules.norms.models import ActivitySeason, Norm

pytestmark = pytest.mark.asyncio


async def _make_activity_season(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    activity_type_id: uuid.UUID,
    season: dict,
    min_term_days: int | None,
    created_by: uuid.UUID,
) -> ActivitySeason:
    row = ActivitySeason(
        organization_id=organization_id,
        activity_type_id=activity_type_id,
        season=season,
        min_term_days=min_term_days,
        created_by=created_by,
    )
    db.add(row)
    await db.flush()
    return row


async def test_effective_season_by_contour_falls_back_to_the_dictionary(
    applicant_client: AsyncClient,
    db: AsyncSession,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    leshoz: Organization,
    gis_user: User,
) -> None:
    await _make_activity_season(
        db,
        organization_id=leshoz.id,
        activity_type_id=grazing_activity_id,
        season={"windows": [{"from": "04-01", "to": "10-31"}]},
        min_term_days=30,
        created_by=gis_user.id,
    )

    response = await applicant_client.get(
        "/api/v1/activity-seasons/effective"
        f"?contour_id={published_contour.id}&activity_type_id={grazing_activity_id}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["organization_id"] == str(leshoz.id)
    assert body["contour_id"] == str(published_contour.id)
    assert body["windows"] == [{"from": "04-01", "to": "10-31"}]
    assert body["season_source"] == "activity_season"
    assert body["min_term_days"] == 30
    assert body["min_term_source"] == "activity_season"


async def test_effective_season_by_contour_prefers_the_norm(
    applicant_client: AsyncClient,
    db: AsyncSession,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    leshoz: Organization,
    gis_user: User,
    approval_doc,
) -> None:
    await _make_activity_season(
        db,
        organization_id=leshoz.id,
        activity_type_id=grazing_activity_id,
        season={"windows": [{"from": "11-01", "to": "03-31"}]},
        min_term_days=None,
        created_by=gis_user.id,
    )
    norm = Norm(
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        season={"windows": [{"from": "04-01", "to": "10-31"}]},
        rotation={"rest_years": []},
        max_sb=100,
        effective_from=date(2020, 1, 1),
        status="published",
        approval_doc_id=approval_doc.id,
        created_by=gis_user.id,
        approved_by=gis_user.id,
    )
    db.add(norm)
    await db.flush()

    response = await applicant_client.get(
        "/api/v1/activity-seasons/effective"
        f"?contour_id={published_contour.id}&activity_type_id={grazing_activity_id}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["windows"] == [{"from": "04-01", "to": "10-31"}]
    assert body["season_source"] == "norm"
    # No norm-level override for the minimum term (ruling #177) — the
    # dictionary's own NULL still means "no minimum enforced".
    assert body["min_term_days"] is None
    assert body["min_term_source"] == "none"


async def test_effective_season_by_organization_needs_no_contour(
    applicant_client: AsyncClient,
    db: AsyncSession,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
    gis_user: User,
) -> None:
    await _make_activity_season(
        db,
        organization_id=leshoz.id,
        activity_type_id=grazing_activity_id,
        season={"windows": [{"from": "04-01", "to": "10-31"}]},
        min_term_days=14,
        created_by=gis_user.id,
    )

    response = await applicant_client.get(
        "/api/v1/activity-seasons/effective"
        f"?organization_id={leshoz.id}&activity_type_id={grazing_activity_id}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contour_id"] is None
    assert body["season_source"] == "activity_season"
    assert body["min_term_days"] == 14


async def test_effective_season_needs_exactly_one_of_contour_or_organization(
    applicant_client: AsyncClient, leshoz: Organization, grazing_activity_id: uuid.UUID
) -> None:
    neither = await applicant_client.get(
        f"/api/v1/activity-seasons/effective?activity_type_id={grazing_activity_id}"
    )
    assert neither.status_code == 422, neither.text
    assert (
        neither.json()["error"]["details"]["reason"]
        == "need_exactly_one_of_contour_or_organization"
    )

    both = await applicant_client.get(
        "/api/v1/activity-seasons/effective"
        f"?activity_type_id={grazing_activity_id}"
        f"&contour_id={uuid.uuid4()}&organization_id={leshoz.id}"
    )
    assert both.status_code == 422, both.text
    assert (
        both.json()["error"]["details"]["reason"] == "need_exactly_one_of_contour_or_organization"
    )


async def test_effective_season_with_neither_source_reports_none(
    applicant_client: AsyncClient, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    response = await applicant_client.get(
        "/api/v1/activity-seasons/effective"
        f"?contour_id={published_contour.id}&activity_type_id={grazing_activity_id}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["windows"] == []
    assert body["season_source"] == "none"
    assert body["min_term_days"] is None
    assert body["min_term_source"] == "none"
