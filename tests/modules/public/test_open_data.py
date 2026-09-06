"""Open data: the layer catalogue (anonymous, `is_public` only) and the
k-anonymity-suppressed stats aggregate (ruling R2, `plans/
04.6-4.8-public-help.md`)."""

from decimal import Decimal

from app.core.models import MediaFile
from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.gis.models import GisLayer
from app.modules.public import service
from tests.conftest import make_client
from tests.modules.gis.conftest import approval_doc as approval_doc  # noqa: F401
from tests.modules.gis.conftest import contours_layer as contours_layer  # noqa: F401
from tests.modules.gis.conftest import leshoz as leshoz  # noqa: F401
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt
from tests.modules.gis.conftest import other_leshoz as other_leshoz  # noqa: F401
from tests.modules.permits.conftest import grazing_activity_id as grazing_activity_id  # noqa: F401
from tests.modules.permits.conftest import make_permit_on_contour

API = "/api/v1"


async def test_the_layer_catalogue_lists_only_public_layers(db):
    async with make_client(create_app(), lifespan=True) as client:
        r = await client.get(f"{API}/public/open-data/layers")
    assert r.status_code == 200
    codes = {row["code"] for row in r.json()}
    assert "forest_fund" in codes  # is_public = True
    assert "contours" not in codes  # is_public = False


async def _active_permits(
    db,
    *,
    org: Organization,
    contours_layer: GisLayer,
    grazing_activity_id,
    approval_doc: MediaFile,
    count: int,
    area_each: Decimal,
):
    contour = await make_contour(db, contours_layer, org)
    version = await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    for _ in range(count):
        await make_permit_on_contour(
            db,
            contour=contour,
            version_id=version.id,
            org=org,
            activity_type_id=grazing_activity_id,
            status="active",
            area_ha=area_each,
        )


async def test_a_small_organization_is_suppressed_but_still_counted_in_the_total(
    db,
    leshoz: Organization,
    other_leshoz: Organization,
    contours_layer: GisLayer,
    grazing_activity_id,
    approval_doc: MediaFile,
):
    # At the threshold (5) — appears in the per-organization breakdown.
    await _active_permits(
        db,
        org=leshoz,
        contours_layer=contours_layer,
        grazing_activity_id=grazing_activity_id,
        approval_doc=approval_doc,
        count=service.OPEN_DATA_K_ANONYMITY,
        area_each=Decimal("1.0000"),
    )
    # Below the threshold — must be omitted from `by_organization` entirely.
    await _active_permits(
        db,
        org=other_leshoz,
        contours_layer=contours_layer,
        grazing_activity_id=grazing_activity_id,
        approval_doc=approval_doc,
        count=2,
        area_each=Decimal("3.0000"),
    )
    await db.commit()

    stats = await service.open_data_stats(db)

    org_ids = {row["organization_id"] for row in stats["by_organization"]}
    assert leshoz.id in org_ids
    assert other_leshoz.id not in org_ids

    # Neither organization is suppressed from the REPUBLIC total.
    our_rows_count = service.OPEN_DATA_K_ANONYMITY + 2
    our_rows_area = Decimal("1.0000") * service.OPEN_DATA_K_ANONYMITY + Decimal("3.0000") * 2
    assert stats["total_active_permits"] >= our_rows_count
    assert stats["total_active_area_ha"] >= our_rows_area
    assert stats["k_anonymity_threshold"] == service.OPEN_DATA_K_ANONYMITY


async def test_open_data_stats_route_is_reachable_anonymously(db):
    async with make_client(create_app(), lifespan=True) as client:
        r = await client.get(f"{API}/public/open-data/stats")
    assert r.status_code == 200
    assert "by_region" in r.json()
