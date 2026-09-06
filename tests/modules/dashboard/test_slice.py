"""`GET /api/v1/dashboard/territory-slice` — k-anonymity suppression (plan
ruling d, default threshold 5) at the contour level, where it almost always
bites."""

from datetime import UTC, datetime

from app.modules.dashboard.permissions import DASHBOARD_VIEW
from tests.modules.gis.conftest import _client_for, make_contour
from tests.modules.gis.conftest import contours_layer as contours_layer  # noqa: F401
from tests.modules.oversight.conftest import make_bare_application

API = "/api/v1"
PERIOD_FROM = "2027-02-01"
PERIOD_TO = "2027-02-28"


async def _client_for_zoned(db, organization_id):
    async for client in _client_for(db, DASHBOARD_VIEW, organization_id=organization_id):
        yield client


async def _submit(db, *, org, contour_id):
    app_row = await make_bare_application(db, org=org, contour_id=contour_id)
    app_row.submitted_at = datetime(2027, 2, 15, tzinfo=UTC)
    await db.flush()
    return app_row


async def test_a_contour_below_threshold_is_suppressed(db, leshoz, contours_layer):
    contour = await make_contour(db, contours_layer, leshoz)
    await _submit(db, org=leshoz, contour_id=contour.id)
    await _submit(db, org=leshoz, contour_id=contour.id)

    async for client in _client_for_zoned(db, leshoz.id):
        response = await client.get(
            f"{API}/dashboard/territory-slice",
            params={
                "organization_id": str(leshoz.id),
                "period_from": PERIOD_FROM,
                "period_to": PERIOD_TO,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["level"] == "contour"
        assert body["k_anonymity_threshold"] == 5
        cell = next(c for c in body["cells"] if c["key"] == str(contour.id))
        assert cell["suppressed"] is True
        assert cell["applicant_count"] is None
        assert cell["applications_count"] is None


async def test_a_contour_at_threshold_is_shown(db, leshoz, contours_layer):
    contour = await make_contour(db, contours_layer, leshoz)
    for _ in range(5):
        await _submit(db, org=leshoz, contour_id=contour.id)

    async for client in _client_for_zoned(db, leshoz.id):
        response = await client.get(
            f"{API}/dashboard/territory-slice",
            params={
                "organization_id": str(leshoz.id),
                "period_from": PERIOD_FROM,
                "period_to": PERIOD_TO,
            },
        )
        body = response.json()
        cell = next(c for c in body["cells"] if c["key"] == str(contour.id))
        assert cell["suppressed"] is False
        assert cell["applicant_count"] == 5
        assert cell["applications_count"] == 5


async def test_no_filters_returns_the_region_level(db, leshoz):
    async for client in _client_for_zoned(db, None):
        response = await client.get(
            f"{API}/dashboard/territory-slice",
            params={"period_from": PERIOD_FROM, "period_to": PERIOD_TO},
        )
        assert response.status_code == 200
        assert response.json()["level"] == "region"
