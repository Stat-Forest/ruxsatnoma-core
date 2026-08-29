"""Every query and every PostGIS predicate of the gis module. Geometry never
travels through Python: the repo builds SQL, PostGIS evaluates it."""

import json
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.db import uuid7
from app.modules.gis.models import Contour, ContourVersion, GisLayer


async def list_layers(db: AsyncSession) -> list[GisLayer]:
    result = await db.execute(select(GisLayer).order_by(GisLayer.code))
    return list(result.scalars().all())


async def layer_by_code(db: AsyncSession, code: str) -> GisLayer | None:
    result = await db.execute(select(GisLayer).where(GisLayer.code == code))
    return result.scalar_one_or_none()


def normalised(geojson_param: Any) -> Any:
    """Ruling 10, as one SQL expression: force 2D (the source is PolygonZ with a
    vertical datum we ignore), repair self-intersections, keep only polygonal
    parts, wrap as MultiPolygon. Evaluated by PostGIS — never in Python."""
    return func.ST_Multi(
        func.ST_CollectionExtract(
            func.ST_MakeValid(
                func.ST_Force2D(func.ST_SetSRID(func.ST_GeomFromGeoJSON(geojson_param), 4326))
            ),
            3,
        )
    )


AREA_HA = "ST_Area(geom::geography) / 10000.0"


async def insert_version(
    db: AsyncSession,
    *,
    contour_id: uuid.UUID,
    version_no: int,
    geojson: dict[str, Any],
    source: str,
    created_by: uuid.UUID | None,
    declared_area_ha: Decimal | None = None,
    accuracy_m: Decimal | None = None,
    survey_date: date | None = None,
    effective_from: date | None = None,
    approval_doc_id: uuid.UUID | None = None,
    import_id: uuid.UUID | None = None,
    status: str = "draft",
) -> ContourVersion:
    """Insert one version, computing geometry and area in the database. A geometry
    that normalises to nothing (a line, a point, an empty collection) is
    ERR-GIS-001 — never an empty row.

    Keyword-only, `geojson` included, and every other geometry-adjacent keyword
    left out entirely rather than defaulted to None: Task 7 (file import) adds a
    second, mutually exclusive way to supply geometry (`wkb=` + `srid=`, since
    reprojection happens in PostGIS) alongside this one, and this shape makes
    that a pure addition — no existing caller has to change.
    """
    row = (
        await db.execute(
            text(
                "INSERT INTO contour_versions (id, contour_id, version_no, geom, area_ha,"
                " declared_area_ha, source, accuracy_m, survey_date, effective_from,"
                " approval_doc_id, import_id, status, created_by)"
                " SELECT :id, :contour_id, :version_no, g,"
                " ROUND((ST_Area(g::geography)/10000.0)::numeric, 4),"
                " :declared_area_ha, :source, :accuracy_m, :survey_date, :effective_from,"
                " :approval_doc_id, :import_id, :status, :created_by"
                " FROM (SELECT ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Force2D("
                "   ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326))), 3)) AS g) AS n"
                " WHERE g IS NOT NULL AND NOT ST_IsEmpty(g)"
                " RETURNING id"
            ),
            {
                "id": uuid7(),
                "contour_id": contour_id,
                "version_no": version_no,
                "geojson": json.dumps(geojson),
                "declared_area_ha": declared_area_ha,
                "source": source,
                "accuracy_m": accuracy_m,
                "survey_date": survey_date,
                "effective_from": effective_from,
                "approval_doc_id": approval_doc_id,
                "import_id": import_id,
                "status": status,
                "created_by": created_by,
            },
        )
    ).scalar_one_or_none()
    if row is None:
        raise err("ERR-GIS-001", details={"reason": "not_polygonal"})
    version = await db.get(ContourVersion, row)
    assert version is not None  # just inserted in this transaction
    return version


async def next_version_no(db: AsyncSession, contour_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(ContourVersion.version_no), 0) + 1).where(
            ContourVersion.contour_id == contour_id
        )
    )
    return result.scalar_one()


async def contour_by_id(db: AsyncSession, contour_id: uuid.UUID) -> Contour | None:
    return await db.get(Contour, contour_id)


async def published_version(db: AsyncSession, contour_id: uuid.UUID) -> ContourVersion | None:
    result = await db.execute(
        select(ContourVersion).where(
            ContourVersion.contour_id == contour_id, ContourVersion.status == "published"
        )
    )
    return result.scalar_one_or_none()
