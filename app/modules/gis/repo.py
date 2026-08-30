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
from app.modules.gis.models import Contour, ContourVersion, GisLayer, LayerFeature


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


async def version_by_id(db: AsyncSession, version_id: uuid.UUID) -> ContourVersion | None:
    return await db.get(ContourVersion, version_id)


async def published_version(db: AsyncSession, contour_id: uuid.UUID) -> ContourVersion | None:
    result = await db.execute(
        select(ContourVersion).where(
            ContourVersion.contour_id == contour_id, ContourVersion.status == "published"
        )
    )
    return result.scalar_one_or_none()


# --- Task 6: layer_features (restriction, protection, fire-ban and every
# other non-contour layer object) --------------------------------------------
#
# `layer_features.geom` is plain GEOMETRY, not MULTIPOLYGON: this catalogue
# also holds points (`water_points`) and lines (`cattle_corridors`), which
# `normalised()`/`insert_version`'s own `ST_CollectionExtract(..., 3)` above
# would silently discard. The pipeline below stops one step earlier — force
# 2D, repair self-intersections, wrap as Multi* — and the RESULT's own type is
# validated by the caller (`gis.service.create_feature`) against the layer's
# declared `geometry_type` instead (task-6 brief's design note). Do not reuse
# `normalised()`/`insert_version`'s expression unchanged for this table.

GEOMETRY_TYPE_FAMILIES: dict[str, tuple[str, ...]] = {
    "POINT": ("ST_Point", "ST_MultiPoint"),
    "LINESTRING": ("ST_LineString", "ST_MultiLineString"),
    "POLYGON": ("ST_Polygon", "ST_MultiPolygon"),
    "MULTIPOLYGON": ("ST_Polygon", "ST_MultiPolygon"),
    "GEOMETRY": (),  # anything goes — the `restrictions` layer is deliberately mixed
}


def _feature_geom(geojson: dict[str, Any]) -> Any:
    """The ONE normalisation expression shared by `feature_geometry_type` and
    `insert_feature` below — defined once so the type the former VALIDATES is
    provably the same geometry the latter INSERTS. Two independent copies of
    this computation would risk exactly the kind of silent divergence this
    project has already hit once (lesson: `checks.jsonable`'s history as two
    near-identical local copies that had already drifted apart by the time a
    review caught it)."""
    return func.ST_Multi(
        func.ST_MakeValid(
            func.ST_Force2D(func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(geojson)), 4326))
        )
    )


async def feature_geometry_type(db: AsyncSession, geojson: dict[str, Any]) -> str | None:
    """PostGIS's own `ST_GeometryType()` for `_feature_geom`, computed WITHOUT
    inserting anything — lets `gis.service.create_feature` read the layer's
    own `geometry_type`, resolve its family from `GEOMETRY_TYPE_FAMILIES` and
    compare before writing a row. `None` means the geometry normalised to
    nothing (an empty/degenerate input, distinct from a type mismatch — the
    caller reports each as its own reason). Only a short type name (e.g.
    'ST_MultiPoint') ever crosses into Python, never the geometry itself
    (module docstring: PostGIS evaluates every predicate).

    A malformed GeoJSON dict (unparseable, not just the wrong shape) makes
    `ST_GeomFromGeoJSON` raise inside this SELECT — that surfaces to the
    caller as a `DBAPIError`, exactly like `insert_version`'s own parse
    failure, and poisons the session the same way; the caller is responsible
    for catching it there, not here.
    """
    g = _feature_geom(geojson)
    geometry_type, is_empty = (
        await db.execute(select(func.ST_GeometryType(g), func.ST_IsEmpty(g)))
    ).one()
    return geometry_type if is_empty is False else None


async def insert_feature(
    db: AsyncSession,
    *,
    layer_id: uuid.UUID,
    geojson: dict[str, Any],
    organization_id: uuid.UUID | None,
    created_by: uuid.UUID | None,
    name: dict[str, Any] | None = None,
    props: dict[str, Any] | None = None,
    valid_from: date | None = None,
    valid_to: date | None = None,
    approval_doc_id: uuid.UUID | None = None,
    import_id: uuid.UUID | None = None,
    status: str = "draft",
) -> LayerFeature:
    """Insert one feature, geometry normalised in the database via the SAME
    `_feature_geom` expression `feature_geometry_type` already validated —
    mirrors `insert_version`'s own division of labour (geometry construction
    stays entirely in PostGIS; Python only supplies the GeoJSON and reads back
    a fully-formed row). Built through the ORM rather than a hand-written
    `INSERT ... RETURNING`, unlike `insert_version`: `name`/`props` are JSONB,
    and `LayerFeature`'s own mapped column type already serialises a plain
    Python dict correctly on every other write path in this module (e.g.
    `gis_layer.style`) — hand-binding a JSONB value through raw `text()` is a
    new, unproven pattern in application code (only Alembic migrations have
    needed the `json.dumps` + `CAST(... AS jsonb)` workaround the lessons file
    describes for that lower-level API), so this reuses the already-correct
    path instead of introducing a second one for `geom` alone to justify.

    Keyword-only, and `approval_doc_id`/`import_id` accepted (defaulted to
    `None`) even though `FeatureIn` exposes neither today: Task 7's bulk
    importer is expected to insert through this exact function with
    `import_id` set, and this shape makes that a pure addition later, not a
    signature change — same reasoning `insert_version` documents for its own
    keyword-only shape.

    The caller validates the geometry (type and non-emptiness) via
    `feature_geometry_type` BEFORE calling this — so `geom` here is never
    empty in normal use; still built as a database-evaluated expression, never
    a Python-side geometry value, per this module's own docstring.
    """
    feature = LayerFeature(
        layer_id=layer_id,
        organization_id=organization_id,
        geom=_feature_geom(geojson),
        name=name,
        props=props if props is not None else {},
        valid_from=valid_from,
        valid_to=valid_to,
        approval_doc_id=approval_doc_id,
        import_id=import_id,
        status=status,
        created_by=created_by,
    )
    db.add(feature)
    await db.flush()
    # geom was assigned as a SQL expression, not a Python value — refresh to
    # read back what PostGIS actually stored (mirrors conftest's make_version;
    # same care the "onupdate columns are left expired" lesson describes,
    # applied here to a server-evaluated INSERT expression instead).
    await db.refresh(feature)
    return feature


async def feature_by_id(db: AsyncSession, feature_id: uuid.UUID) -> LayerFeature | None:
    return await db.get(LayerFeature, feature_id)
