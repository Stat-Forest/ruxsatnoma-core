"""Burchmulla, end to end: publish the conditional-head coefficients, draft and
publish a norm for a real contour, then compute a grazing fee and watch the limit
bite. This is the scenario stage 3.9 will drive from an application."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import MediaFile
from app.db import make_session_factory
from app.modules.gis.models import Contour
from app.modules.norms import service
from app.modules.norms.schemas import CalculationIn, LivestockItemIn
from tests.modules.norms.conftest import CONTOUR_AREA_HA

pytestmark = pytest.mark.asyncio


async def test_a_leshoz_publishes_a_norm_and_an_applicant_gets_a_priced_answer(
    tariffs_maker_client: AsyncClient,
    tariffs_checker_client: AsyncClient,
    gis_specialist_client: AsyncClient,
    leadership_client: AsyncClient,
    central_admin_client: AsyncClient,
    applicant_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    survey_doc: MediaFile,
    approval_doc: MediaFile,
    frozen_on_date: date,
    engine,
    db: AsyncSession,
) -> None:
    """`frozen_on_date` pins `on_date` inside the 412 000 `bhm` window, the same
    reason `test_preview_api.py`/`test_calculations_api.py`'s own exact-amount
    tests need it (lesson: the expected amount is only correct on one side of
    the seeded 2026-09-01 switch). Neither `norms.checks` nor `norms.params`
    call `business_today()` themselves — both only ever see the
    already-resolved `request.on_date` — so patching `norms.service`'s own
    binding is enough for the whole scenario, norm publication included
    (`publish_norm` never reads `business_today()` at all; it prices the
    limit off the norm's own `effective_from`)."""
    # 1. The Agency's annex 5 arrives: the central office publishes the
    #    conditional-head coefficient this scenario needs (it ships as a
    #    draft, ruling 8). The seeded `coef_sb:*` rows are SHARED, SINGLETON
    #    rows — publishing this one through the real API and leaving it
    #    published would permanently break
    #    test_seeds.py::test_conditional_head_coefficients_are_seeded_as_drafts
    #    for every later run (already hit twice in this stage), so it is
    #    restored to `draft` in `finally`, on its OWN session
    #    (`make_session_factory(engine)`), never the test's own rollback-only
    #    `db` fixture — the publish below runs through the app's own real
    #    session (an httpx call against the ASGI app commits for real, lesson),
    #    so only a genuine UPDATE against the shared engine undoes it.
    listed = await tariffs_maker_client.get("/api/v1/rule-parameters?code=coef_sb:sheep_goat_6m")
    draft_id = listed.json()["items"][0]["id"]
    try:
        published = await tariffs_checker_client.post(f"/api/v1/rule-parameters/{draft_id}/publish")
        assert published.status_code == 200

        # 2. The leshoz drafts the norm off its geobotanical survey, the raҳbar
        #    approves it, the central office puts it in force (ruling 16).
        norm = await gis_specialist_client.post(
            "/api/v1/norms",
            json={
                "contour_id": str(published_contour.id),
                "activity_type_id": str(grazing_activity_id),
                "yield_c_per_ha": "12.0",
                "season": {"windows": [{"from": "04-01", "to": "10-31"}]},
                "rotation": {"rest_years": [2027]},
                "geobotanic_doc_id": str(survey_doc.id),
                "effective_from": "2026-01-01",
            },
        )
        norm_id = norm.json()["id"]
        await gis_specialist_client.post(f"/api/v1/norms/{norm_id}/submit-review")
        await leadership_client.post(
            f"/api/v1/norms/{norm_id}/approve", json={"approval_doc_id": str(approval_doc.id)}
        )
        in_force = await central_admin_client.post(f"/api/v1/norms/{norm_id}/publish")
        max_sb = in_force.json()["max_sb"]
        assert max_sb == int(CONTOUR_AREA_HA * Decimal("12.0") * Decimal("0.85") / Decimal("3.74"))

        # 3. An applicant prices a summer of grazing inside the season window.
        preview = await applicant_client.post(
            "/api/v1/calculations/preview",
            json={
                "contour_id": str(published_contour.id),
                "activity_type_id": str(grazing_activity_id),
                "period_from": "2026-05-01",
                "period_to": "2026-09-30",
                "items": [{"livestock_code": "sheep_goat_6m", "count": 50}],
            },
        )
        body = preview.json()
        assert body["amount"] == "2060000"  # 50 × 0.1 × 412 000
        assert body["used_sb"] == "50.0"
        assert all(check["result"] in {"pass", "skipped", "warning"} for check in body["checks"])

        # 4. The same request in the plot's rest year is refused by the rotation rule.
        rest_year = await applicant_client.post(
            "/api/v1/calculations/preview",
            json={
                "contour_id": str(published_contour.id),
                "activity_type_id": str(grazing_activity_id),
                "period_from": "2027-05-01",
                "period_to": "2027-09-30",
                "items": [{"livestock_code": "sheep_goat_6m", "count": 50}],
            },
        )
        rotation = next(c for c in rest_year.json()["checks"] if c["check"] == "rotation")
        assert rotation["result"] == "fail"
        assert rotation["details"]["reason"] == "rest_year"

        # 5. Saving that calculation is refused — a preview reports, a save commits.
        refused = await applicant_client.post(
            "/api/v1/calculations",
            json={
                "contour_id": str(published_contour.id),
                "activity_type_id": str(grazing_activity_id),
                "period_from": "2027-05-01",
                "period_to": "2027-09-30",
                "items": [{"livestock_code": "sheep_goat_6m", "count": 50}],
            },
        )
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "ERR-NORM-003"

        # 6. The same facts, read the way 3.9/3.11 will actually read them — in
        #    process, never over HTTP. `effective_norm` is what a permit (3.11)
        #    reads its own limit through; `run_checks` is a reviewer's screen
        #    (3.9) that must see the identical rotation refusal WITHOUT ever
        #    pricing the request — `used_sb` stays unresolved (`skipped`,
        #    `not_computed`), never a manufactured zero.
        in_force_norm = await service.effective_norm(
            db, published_contour.id, grazing_activity_id, frozen_on_date
        )
        assert in_force_norm is not None
        assert in_force_norm.id == uuid.UUID(norm_id)
        assert in_force_norm.max_sb == max_sb

        unpriced = await service.run_checks(
            db,
            payload=CalculationIn(
                contour_id=published_contour.id,
                activity_type_id=grazing_activity_id,
                period_from=date(2027, 5, 1),
                period_to=date(2027, 9, 30),
                items=[LivestockItemIn(livestock_code="sheep_goat_6m", count=50)],
            ),
        )
        unpriced_rotation = next(c for c in unpriced if c["check"] == "rotation")
        assert unpriced_rotation["result"] == "fail"
        assert unpriced_rotation["details"]["reason"] == "rest_year"
        limit_check = next(c for c in unpriced if c["check"] == "limit")
        assert limit_check == {
            "check": "limit",
            "result": "skipped",
            "details": {"reason": "not_computed"},
        }
    finally:
        factory = make_session_factory(engine)
        async with factory() as own_db:
            await own_db.execute(
                text(
                    "UPDATE rule_parameters SET status = 'draft', approved_by = NULL WHERE id = :id"
                ).bindparams(id=uuid.UUID(draft_id))
            )
            await own_db.commit()
