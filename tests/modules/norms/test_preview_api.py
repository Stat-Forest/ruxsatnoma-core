"""`POST /calculations/preview` — the calculator behind an endpoint. It changes
nothing, so a failing check is reported INSIDE the body, not as an HTTP error
(design/03: "ERR-NORM-001..003 inside `checks`, not as an HTTP error")."""

import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.gis.models import Contour

pytestmark = pytest.mark.asyncio


async def test_a_haymaking_preview_returns_the_amount_and_the_checks(
    applicant_client: AsyncClient,
    published_contour: Contour,
    haymaking_activity_id: uuid.UUID,
    frozen_on_date: date,
) -> None:
    """`frozen_on_date` pins `on_date` inside the 412 000 `bhm` window — the
    seeded rate switches to 440 000 on 2026-09-01, and this test's own
    expected amount is only correct on one side of that date (lesson)."""
    response = await applicant_client.post(
        "/api/v1/calculations/preview",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(haymaking_activity_id),
            "period_from": "2026-06-01",
            "period_to": "2026-09-30",
            "quantity": "4",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["amount"] == "2472000"  # 4 ha × 1.5 × 412 000
    assert {c["check"] for c in body["checks"]} >= {"norm", "fire_ban", "restrictions"}
    assert body["rule_code_version"] == "norms-1.0.0"


async def test_a_grazing_preview_reports_the_missing_coefficient_rather_than_guessing(
    applicant_client: AsyncClient, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """The seeded `coef_sb:*` rows are drafts (ruling 8), so on a fresh database
    this is the honest answer — and it names the parameter."""
    response = await applicant_client.post(
        "/api/v1/calculations/preview",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(grazing_activity_id),
            "period_from": "2026-05-01",
            "period_to": "2026-09-30",
            "items": [{"livestock_code": "sheep_goat_6m", "count": 50}],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR-NORM-004"
    assert response.json()["error"]["details"]["code"].startswith("coef_sb:")


async def test_a_preview_over_the_limit_reports_the_failure_without_refusing(
    applicant_client: AsyncClient,
    published_grazing_norm: uuid.UUID,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    published_coef_sb: None,
) -> None:
    """A preview is a dry run: it must SHOW that the limit is exceeded, not 422."""
    response = await applicant_client.post(
        "/api/v1/calculations/preview",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(grazing_activity_id),
            "period_from": "2026-05-01",
            "period_to": "2026-09-30",
            "items": [{"livestock_code": "cattle_adult", "count": 500}],
        },
    )
    assert response.status_code == 200
    limit = next(c for c in response.json()["checks"] if c["check"] == "limit")
    assert limit["result"] == "fail"
    assert limit["details"]["used_sb"] == "3000.0"


async def test_a_preview_writes_nothing(
    applicant_client: AsyncClient,
    db: AsyncSession,
    published_contour: Contour,
    haymaking_activity_id: uuid.UUID,
) -> None:
    before = (await db.execute(text("SELECT count(*) FROM calculations"))).scalar_one()
    await applicant_client.post(
        "/api/v1/calculations/preview",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(haymaking_activity_id),
            "period_from": "2026-06-01",
            "period_to": "2026-09-30",
            "quantity": "4",
        },
    )
    after = (await db.execute(text("SELECT count(*) FROM calculations"))).scalar_one()
    assert after == before
