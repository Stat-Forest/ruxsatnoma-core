"""`permits.service.public_active_stats_by_organization` — the anonymous
open-data read 4.6 `public` aggregates (design/01 rule 2: this is the ONE
door `public` may use into this module)."""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import MediaFile
from app.modules.admin.models import Organization
from app.modules.gis.models import GisLayer
from app.modules.permits import service
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt
from tests.modules.permits.conftest import make_permit_on_contour


async def test_counts_and_sums_only_active_permits_grouped_by_organization(
    db: AsyncSession,
    leshoz: Organization,
    contours_layer: GisLayer,
    grazing_activity_id,
    approval_doc: MediaFile,
):
    contour = await make_contour(db, contours_layer, leshoz)
    version = await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version.id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="active",
        area_ha=Decimal("10.0000"),
    )
    await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version.id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="active",
        area_ha=Decimal("5.5000"),
    )
    # Not active — must not be counted at all.
    await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version.id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="expired",
        area_ha=Decimal("99.0000"),
    )
    await db.commit()

    rows = await service.public_active_stats_by_organization(db)
    row = next(r for r in rows if r["organization_id"] == leshoz.id)
    assert row["active_count"] == 2
    assert row["active_area_ha"] == Decimal("15.5000")
