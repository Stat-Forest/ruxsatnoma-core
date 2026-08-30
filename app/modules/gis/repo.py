"""Every query and every PostGIS predicate of the gis module. Geometry never
travels through Python: the repo builds SQL, PostGIS evaluates it."""

import json
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.db import uuid7

# `Organization` (region_id/district_id) is read-only here, for the zone JOIN
# `list_contours` needs — gis is one of the modules CLAUDE.md's module-
# boundaries rule explicitly grants read-only table access to reference data,
# alongside the reporting/dashboard/search readers.
from app.modules.admin.models import Organization
from app.modules.gis.models import Contour, ContourVersion, GisImport, GisLayer, LayerFeature


async def list_layers(db: AsyncSession) -> list[GisLayer]:
    result = await db.execute(select(GisLayer).order_by(GisLayer.code))
    return list(result.scalars().all())


async def layer_by_code(db: AsyncSession, code: str) -> GisLayer | None:
    result = await db.execute(select(GisLayer).where(GisLayer.code == code))
    return result.scalar_one_or_none()


async def layer_by_id(db: AsyncSession, layer_id: uuid.UUID) -> GisLayer | None:
    """The catalogue row a request named by id — `POST /gis/contours` takes
    `layer_id` in its body, not a code."""
    return await db.get(GisLayer, layer_id)


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

# The two ways geometry enters this module, as SQL. The API path (Task 3) posts
# GeoJSON, already in WGS84; the import path (Task 7) hands over the source
# file's own WKB plus the SRID `gis.importer.parse` read off it, and PostGIS
# does the reprojection — decision #13, with exactly ONE reprojection engine in
# the system rather than GDAL's answer and PostGIS's answer side by side.
# Both are constants chosen by an `if`, never interpolated from caller data.
_GEOJSON_SOURCE = "ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326)"
_WKB_SOURCE = "ST_Transform(ST_SetSRID(ST_GeomFromWKB(:wkb), :srid), 4326)"


def _geom_source_sql(
    geojson: dict[str, Any] | None, wkb: bytes | None, srid: int
) -> tuple[str, dict[str, Any]]:
    """Pick the geometry-source fragment and its own bind parameters.

    Exactly one of `geojson`/`wkb` must be supplied; neither or both is an
    `AssertionError`, not a domain error — no request body can produce it, only
    a miswritten call.
    """
    assert (geojson is None) != (wkb is None), "supply exactly one of geojson= or wkb="
    if geojson is not None:
        return _GEOJSON_SOURCE, {"geojson": json.dumps(geojson)}
    return _WKB_SOURCE, {"wkb": wkb, "srid": srid}


async def insert_version(
    db: AsyncSession,
    *,
    contour_id: uuid.UUID,
    version_no: int,
    geojson: dict[str, Any] | None = None,
    wkb: bytes | None = None,
    srid: int = 4326,
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

    Geometry arrives EITHER as `geojson` (the API path, already WGS84) OR as
    `wkb=` + `srid=` (the import path — `gis.importer` reads the source file's
    own projection and lets PostGIS transform it). Exactly one; neither or both
    is an `AssertionError`. Both feed the SAME normalisation expression below,
    so an imported version and a hand-drawn one are repaired identically.
    """
    geom_sql, geom_params = _geom_source_sql(geojson, wkb, srid)
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
                # Bandit flags this as B608 (string-built SQL) on the pattern
                # alone; `geom_sql` is always one of the two module constants
                # above, chosen by an `if`, never caller input — every actual
                # value crosses the wire bound, through `geom_params` below.
                # Same reasoning (and the same nosec) as `checks._intersections`.
                f"   {geom_sql})), 3)) AS g) AS n"  # nosec B608
                " WHERE g IS NOT NULL AND NOT ST_IsEmpty(g)"
                " RETURNING id"
            ),
            {
                "id": uuid7(),
                "contour_id": contour_id,
                "version_no": version_no,
                **geom_params,
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


def _feature_source(geojson: dict[str, Any] | None, wkb: bytes | None, srid: int) -> Any:
    """`_geom_source_sql`'s counterpart for this table, as a SQLAlchemy
    construct instead of a raw fragment (this table is written through the ORM,
    `contour_versions` through `text()` — see `insert_feature`'s own note on
    why). Same rule: exactly one of `geojson`/`wkb`, neither or both is an
    `AssertionError`."""
    assert (geojson is None) != (wkb is None), "supply exactly one of geojson= or wkb="
    if geojson is not None:
        return func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(geojson)), 4326)
    return func.ST_Transform(func.ST_SetSRID(func.ST_GeomFromWKB(wkb), srid), 4326)


def _feature_geom(source: Any) -> Any:
    """The ONE normalisation expression shared by `feature_geometry_type` and
    `insert_feature` below — defined once so the type the former VALIDATES is
    provably the same geometry the latter INSERTS. Two independent copies of
    this computation would risk exactly the kind of silent divergence this
    project has already hit once (lesson: `checks.jsonable`'s history as two
    near-identical local copies that had already drifted apart by the time a
    review caught it).

    Takes the already-SRID-resolved source expression (`_feature_source`) rather
    than GeoJSON, so Task 7's import path — WKB plus the file's own SRID,
    reprojected by PostGIS — reuses this same normalisation instead of adding a
    third copy of it."""
    return func.ST_Multi(func.ST_MakeValid(func.ST_Force2D(source)))


async def feature_geometry_type(
    db: AsyncSession,
    geojson: dict[str, Any] | None = None,
    *,
    wkb: bytes | None = None,
    srid: int = 4326,
) -> str | None:
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
    g = _feature_geom(_feature_source(geojson, wkb, srid))
    geometry_type, is_empty = (
        await db.execute(select(func.ST_GeometryType(g), func.ST_IsEmpty(g)))
    ).one()
    return geometry_type if is_empty is False else None


async def insert_feature(
    db: AsyncSession,
    *,
    layer_id: uuid.UUID,
    geojson: dict[str, Any] | None = None,
    wkb: bytes | None = None,
    srid: int = 4326,
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
    importer inserts through this exact function with `import_id` set, and
    this shape made that a pure addition rather than a signature change —
    same reasoning `insert_version` documents for its own keyword-only shape.

    Geometry arrives EITHER as `geojson` (the API path) OR as `wkb=` + `srid=`
    (the import path), exactly like `insert_version`; `_feature_source` picks
    between them and `_feature_geom` normalises whichever it gets, so an
    imported feature and a hand-drawn one are repaired identically.

    The caller validates the geometry (type and non-emptiness) via
    `feature_geometry_type` BEFORE calling this — so `geom` here is never
    empty in normal use; still built as a database-evaluated expression, never
    a Python-side geometry value, per this module's own docstring.
    """
    feature = LayerFeature(
        layer_id=layer_id,
        organization_id=organization_id,
        geom=_feature_geom(_feature_source(geojson, wkb, srid)),
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


# --- Task 7: the import queue ------------------------------------------------


async def claim_pending_import(db: AsyncSession) -> GisImport | None:
    """Claim the oldest `pending` import; the row lock is held until the caller
    commits or rolls back.

    The same idiom as `integrations.repo.pick_due` (the outbox worker's claim):
    SKIP LOCKED lets any number of worker processes drain the queue without
    stepping on each other. Deliberately NOT the outbox itself — the outbox
    carries messages LEAVING the system, this is inbound work (ruling 6).
    """
    return (
        await db.execute(
            select(GisImport)
            .where(GisImport.status == "pending")
            .order_by(GisImport.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()


async def import_by_id(db: AsyncSession, import_id: uuid.UUID) -> GisImport | None:
    return await db.get(GisImport, import_id)


async def contour_numbers(db: AsyncSession, organization_id: uuid.UUID) -> set[str]:
    """Every contour number already taken inside one organization — read ONCE
    per import so the `/2`, `/3` suffixing of ruling 11 is decided in Python
    against a single snapshot instead of one SELECT per feature (151 of them in
    the real Burchmulla delivery)."""
    rows = await db.execute(
        select(Contour.number).where(Contour.organization_id == organization_id)
    )
    return set(rows.scalars().all())


async def import_versions(
    db: AsyncSession, import_id: uuid.UUID, *, status: str
) -> list[ContourVersion]:
    """Every version ONE import batch created, at a given status — what
    `gis.service.submit_import_review`/`approve_import`/`publish_import` each
    loop over, one status per call so a batch action never touches a version
    already past (or not yet at) the stage it is meant for."""
    result = await db.execute(
        select(ContourVersion)
        .where(ContourVersion.import_id == import_id, ContourVersion.status == status)
        .order_by(ContourVersion.created_at)
    )
    return list(result.scalars().all())


# --- Task 8: the read API for 3.7/3.9 -----------------------------------------


async def list_contours(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID | None,
    bbox: tuple[float, float, float, float] | None,
    zone: Any,
) -> list[Any]:
    """One row (contour_id, number, organization_id, version_id, area_ha) per
    contour that HAS a published version, matching the given filters. `zone`
    is whatever `abac.zone_filter` built — always given, `true()` when the
    actor carries no zone at all (a republic-wide staff member, or any
    applicant). Joins `organizations` (review finding 2) so a REGION- or
    DISTRICT-scoped actor's zone can be enforced too, not just the
    organization axis: `Contour` itself carries no region_id/district_id of
    its own, and `zone_filter` fails closed (raises) rather than silently
    under-enforcing when a set zone axis has no column to check it against.
    Geometry is read only as a bbox PREDICATE (`ST_Intersects`), never
    selected into Python (module docstring)."""
    conditions: list[Any] = [ContourVersion.status == "published", zone]
    if organization_id is not None:
        conditions.append(Contour.organization_id == organization_id)
    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
        conditions.append(
            func.ST_Intersects(
                ContourVersion.geom,
                func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326),
            )
        )
    result = await db.execute(
        select(
            Contour.id.label("contour_id"),
            Contour.number,
            Contour.organization_id,
            ContourVersion.id.label("version_id"),
            ContourVersion.area_ha,
        )
        .join(ContourVersion, ContourVersion.contour_id == Contour.id)
        .join(Organization, Organization.id == Contour.organization_id)
        .where(*conditions)
        .order_by(Contour.number)
    )
    return list(result.all())


async def contour_card(db: AsyncSession, contour_id: uuid.UUID) -> Any | None:
    """The published version's card: identity + area + geometry
    (`ST_AsGeoJSON`, computed in SQL — only the resulting STRING crosses into
    Python, module docstring). `None` when the contour has no published
    version (or does not exist at all) — the service turns that into
    `ERR-SYS-003`."""
    result = await db.execute(
        select(
            Contour.id.label("contour_id"),
            Contour.number,
            Contour.organization_id,
            Contour.kind,
            ContourVersion.id.label("version_id"),
            ContourVersion.area_ha,
            func.ST_AsGeoJSON(ContourVersion.geom).label("geometry"),
        )
        .join(ContourVersion, ContourVersion.contour_id == Contour.id)
        .where(Contour.id == contour_id, ContourVersion.status == "published")
    )
    return result.one_or_none()


async def features_geojson(
    db: AsyncSession,
    *,
    layer_code: str,
    bbox: tuple[float, float, float, float] | None,
    valid_on: date | None,
) -> dict[str, Any]:
    """The layer's published features as one GeoJSON FeatureCollection, built
    entirely in SQL (`ST_AsGeoJSON` per row, decision 6): geometry crosses
    into Python only as the string PostGIS already rendered, never as a value
    this module reconstructs itself (module docstring). `valid_on`, when
    given, keeps only features whose validity period (if any) contains that
    date — the same window `checks._RESTRICTIONS_CANDIDATES_SQL` gates
    `fire_bans` on, generalised here to every feature since most carry no
    period at all (`valid_from`/`valid_to` both NULL means "always valid")."""
    conditions: list[Any] = [GisLayer.code == layer_code, LayerFeature.status == "published"]
    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
        conditions.append(
            func.ST_Intersects(
                LayerFeature.geom,
                func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326),
            )
        )
    if valid_on is not None:
        conditions.append(
            or_(LayerFeature.valid_from.is_(None), LayerFeature.valid_from <= valid_on)
        )
        conditions.append(or_(LayerFeature.valid_to.is_(None), LayerFeature.valid_to >= valid_on))
    rows = (
        await db.execute(
            select(
                LayerFeature.id,
                LayerFeature.name,
                LayerFeature.props,
                LayerFeature.valid_from,
                LayerFeature.valid_to,
                func.ST_AsGeoJSON(LayerFeature.geom).label("geometry"),
            )
            .join(GisLayer, GisLayer.id == LayerFeature.layer_id)
            .where(*conditions)
            .order_by(LayerFeature.id)
        )
    ).all()
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": str(row.id),
                "geometry": json.loads(row.geometry),
                "properties": {
                    "name": row.name,
                    "props": row.props,
                    "valid_from": row.valid_from.isoformat() if row.valid_from else None,
                    "valid_to": row.valid_to.isoformat() if row.valid_to else None,
                },
            }
            for row in rows
        ],
    }
