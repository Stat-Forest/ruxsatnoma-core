"""`GET /api/v1/dashboard/kpi` — real sources only (plan ruling g), zone
scoping, and the reversed-period guard (`ERR-VAL-001`, reused rather than a
module error code of its own)."""

from datetime import UTC, date, datetime

from sqlalchemy import select

from app.modules.dashboard.permissions import DASHBOARD_VIEW
from app.modules.gis.models import ContourVersion
from app.modules.permits.models import Permit
from tests.modules.gis.conftest import _client_for, make_contour, make_version, random_box_wkt
from tests.modules.gis.conftest import contours_layer as contours_layer  # noqa: F401
from tests.modules.oversight.conftest import make_bare_application
from tests.modules.permits.conftest import grazing_activity_id as grazing_activity_id  # noqa: F401
from tests.modules.permits.conftest import make_permit_on_contour

API = "/api/v1"

PERIOD_FROM = date(2027, 5, 1)
PERIOD_TO = date(2027, 9, 30)


async def _client_for_zoned(db, organization_id):
    async for client in _client_for(db, DASHBOARD_VIEW, organization_id=organization_id):
        yield client


async def _issue_within_period(db, *, contour, org, activity_type_id, status="active"):
    await make_version(db, contour.id, random_box_wkt())
    version_id = (
        await db.execute(select(ContourVersion.id).where(ContourVersion.contour_id == contour.id))
    ).scalar_one()
    permit = await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=org,
        activity_type_id=activity_type_id,
        status=status,
        period_from=PERIOD_FROM,
        period_to=PERIOD_TO,
    )
    row = await db.get(Permit, permit.id)
    row.issued_at = datetime(2027, 6, 1, tzinfo=UTC)
    await db.flush()
    return permit


async def test_kpi_counts_own_zone_and_names_the_omitted_tiles(
    db, leshoz, other_leshoz, contours_layer, grazing_activity_id
):
    contour = await make_contour(db, contours_layer, leshoz)
    await _issue_within_period(
        db, contour=contour, org=leshoz, activity_type_id=grazing_activity_id
    )

    other_contour = await make_contour(db, contours_layer, other_leshoz)
    await _issue_within_period(
        db, contour=other_contour, org=other_leshoz, activity_type_id=grazing_activity_id
    )

    async for client in _client_for_zoned(db, leshoz.id):
        response = await client.get(
            f"{API}/dashboard/kpi",
            params={"period_from": PERIOD_FROM.isoformat(), "period_to": PERIOD_TO.isoformat()},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["permits"]["issued_count"] == 1
        assert body["permits"]["active_count"] == 1
        assert any("inspections_count" in item for item in body["omitted"])
        assert any("violations_count" in item for item in body["omitted"])


async def test_kpi_reversed_period_is_refused(db, leshoz):
    async for client in _client_for_zoned(db, leshoz.id):
        response = await client.get(
            f"{API}/dashboard/kpi",
            params={"period_from": "2027-09-30", "period_to": "2027-05-01"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "ERR-VAL-001"


async def test_kpi_applications_by_status(db, leshoz):
    app_row = await make_bare_application(db, org=leshoz)

    async for client in _client_for_zoned(db, leshoz.id):
        response = await client.get(
            f"{API}/dashboard/kpi",
            params={"period_from": "2027-01-01", "period_to": "2027-01-31"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["applications"]["by_status"].get(app_row.status, 0) >= 1
