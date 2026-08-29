"""Ruling 10: every incoming geometry is forced 2D, made valid, reduced to its
polygonal parts and wrapped as MultiPolygon — in SQL, by PostGIS."""

import pytest
from sqlalchemy import func, select

from app.modules.gis import repo
from app.modules.gis.models import ContourVersion


async def test_polygon_z_is_stored_as_2d_multipolygon(db, contours_layer, leshoz, gis_user):
    """The Agency ships PolygonZ with an EGM96 datum we ignore."""
    geojson = {
        "type": "Polygon",
        "coordinates": [
            [[69.9, 41.5, 800], [69.91, 41.5, 800], [69.91, 41.51, 800], [69.9, 41.5, 800]]
        ],
    }
    from tests.modules.gis.conftest import make_contour

    contour = await make_contour(db, contours_layer, leshoz)
    version = await repo.insert_version(
        db,
        contour_id=contour.id,
        version_no=1,
        geojson=geojson,
        source="import",
        created_by=gis_user.id,
    )
    row = await db.execute(
        select(func.ST_GeometryType(ContourVersion.geom), func.ST_NDims(ContourVersion.geom)).where(
            ContourVersion.id == version.id
        )
    )
    geom_type, ndims = row.one()
    assert geom_type == "ST_MultiPolygon"
    assert ndims == 2


async def test_area_is_computed_over_geography_not_degrees(db, contours_layer, leshoz, gis_user):
    """A 0.01 deg square near Tashkent is ~92 ha; the same figure in square degrees
    would be 0.0001 — the mistake that makes the source file's Shape_Area useless."""
    from tests.modules.gis.conftest import box_wkt, make_contour, wkt_to_geojson

    contour = await make_contour(db, contours_layer, leshoz)
    version = await repo.insert_version(
        db,
        contour_id=contour.id,
        version_no=1,
        geojson=await wkt_to_geojson(db, box_wkt(69.9, 41.5)),
        source="survey",
        created_by=gis_user.id,
    )
    await db.refresh(version)
    assert 85 < float(version.area_ha) < 100


async def test_self_intersecting_ring_is_repaired(db, contours_layer, leshoz, gis_user):
    """A bow-tie polygon is the commonest defect in hand-digitised layers."""
    from tests.modules.gis.conftest import make_contour

    bowtie = {
        "type": "Polygon",
        "coordinates": [[[69.9, 41.5], [69.91, 41.51], [69.91, 41.5], [69.9, 41.51], [69.9, 41.5]]],
    }
    contour = await make_contour(db, contours_layer, leshoz)
    version = await repo.insert_version(
        db,
        contour_id=contour.id,
        version_no=1,
        geojson=bowtie,
        source="import",
        created_by=gis_user.id,
    )
    valid = await db.scalar(
        select(func.ST_IsValid(ContourVersion.geom)).where(ContourVersion.id == version.id)
    )
    assert valid is True


async def test_a_line_is_rejected_for_the_contour_layer(db, contours_layer, leshoz, gis_user):
    """CollectionExtract(..., 3) leaves nothing behind — that is an error, not an
    empty geometry silently stored."""
    from app.core.errors import DomainError
    from tests.modules.gis.conftest import make_contour

    contour = await make_contour(db, contours_layer, leshoz)
    with pytest.raises(DomainError) as excinfo:
        await repo.insert_version(
            db,
            contour_id=contour.id,
            version_no=1,
            geojson={"type": "LineString", "coordinates": [[69.9, 41.5], [69.91, 41.51]]},
            source="import",
            created_by=gis_user.id,
        )
    assert excinfo.value.code == "ERR-GIS-001"
