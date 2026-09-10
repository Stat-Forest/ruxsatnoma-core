"""Every query and every PostGIS predicate of the gis module. Geometry never
travels through Python: the repo builds SQL, PostGIS evaluates it."""

import json
import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
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


def _normalized_geom_sql(source_sql: str) -> str:
    """The one normalisation pipeline every incoming geometry passes through
    before it is stored OR compared: `ST_Force2D` drops a Z/M dimension a
    source might carry, `ST_MakeValid` repairs self-intersections,
    `ST_CollectionExtract(..., 3)` keeps polygonal parts only (a
    `GeometryCollection` the repair can produce), and `ST_Multi` makes the
    result a MULTIPOLYGON whether the input was a `Polygon` or already a
    `MultiPolygon`. Factored out of `insert_version` so `split_partition_metrics`
    below can compare two candidate pieces against the SAME repaired shape a
    version would actually be stored as — never a second normalisation that
    could quietly disagree with the first (this module's own "one
    reprojection engine" reasoning, decision #13, applied to geometry repair
    instead of geometry transform)."""
    return f"ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Force2D({source_sql})), 3))"


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
    own projection and lets PostGIS transform it) OR as NEITHER (decision #178:
    a leshoz with no delivered GIS layer files a contour by requisites alone).
    Both `geojson`/`wkb` feed the SAME normalisation expression below, so an
    imported version and a hand-drawn one are repaired identically; the caller
    (`gis.service.create_version`) has already confirmed a positive
    `declared_area_ha` before reaching this branch, since that is the only
    figure left to make `area_ha` (which stays NOT NULL either way) out of.
    """
    if geojson is None and wkb is None:
        row = (
            await db.execute(
                text(
                    "INSERT INTO contour_versions (id, contour_id, version_no, geom, area_ha,"
                    " declared_area_ha, source, accuracy_m, survey_date, effective_from,"
                    " approval_doc_id, import_id, status, created_by)"
                    " VALUES (:id, :contour_id, :version_no, NULL, :declared_area_ha,"
                    " :declared_area_ha, :source, :accuracy_m, :survey_date, :effective_from,"
                    " :approval_doc_id, :import_id, :status, :created_by)"
                    " RETURNING id"
                ),
                {
                    "id": uuid7(),
                    "contour_id": contour_id,
                    "version_no": version_no,
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
        ).scalar_one()
        version = await db.get(ContourVersion, row)
        assert version is not None  # just inserted in this transaction
        return version
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
                " FROM (SELECT "
                # Bandit flags this as B608 (string-built SQL) on the pattern
                # alone; `geom_sql` is always one of the two module constants
                # above, chosen by an `if`, never caller input — every actual
                # value crosses the wire bound, through `geom_params` below.
                # Same reasoning (and the same nosec) as `checks._intersections`.
                f"{_normalized_geom_sql(geom_sql)} AS g) AS n"  # nosec B608
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


async def version_geometry(db: AsyncSession, version_id: uuid.UUID) -> str | None:
    """One version's geometry alone, as the GeoJSON string PostGIS renders
    (`ST_AsGeoJSON`, same conversion `contour_card`/`version_detail` use).

    `None` two different ways since decision #178 made `ContourVersion.geom`
    nullable: no such version exists at all, OR the version exists but was
    imported with no delivered geometry (`geom IS NULL`, and
    `ST_AsGeoJSON(NULL)` is itself SQL `NULL`) — this function cannot and
    does not tell the two apart. A caller that must knows to call
    `version_by_id` instead (or first), which returns the row itself and so
    can.

    Deliberately keyed on the VERSION, not on `status`: a permit's frozen
    `contour_version_id` may no longer be the contour's `published` row (a
    boundary correction or a #91 split republishes it and archives the one
    the permit was actually issued against), and it is that exact row, not
    "whatever is published today", this reads."""
    result = await db.execute(
        select(func.ST_AsGeoJSON(ContourVersion.geom)).where(ContourVersion.id == version_id)
    )
    return result.scalar_one_or_none()


async def has_children(db: AsyncSession, parent_id: uuid.UUID) -> bool:
    """Whether ANY contour already names `parent_id` as its `parent_id` — the
    precondition `service.split_contour` refuses on (decision #91: a split
    contour that already has children is already split; a second split would
    leave the hierarchy ambiguous about which pair of subcontours a later
    reader should trust)."""
    result = await db.execute(select(Contour.id).where(Contour.parent_id == parent_id).limit(1))
    return result.first() is not None


# The two candidate pieces of a split, each bound under its OWN name —
# `_geom_source_sql` above always calls its single geometry parameter
# `:geojson`, which `split_partition_metrics` cannot reuse as-is: it compares
# TWO client-supplied geometries in ONE query, so each needs a distinct bind
# parameter. Same reasoning as `_GEOJSON_SOURCE`/`_WKB_SOURCE`: both are fixed
# module constants, chosen by no caller input, so building the query text
# around them carries no injection risk of its own.
_SPLIT_PIECE_A_SOURCE = "ST_SetSRID(ST_GeomFromGeoJSON(:geojson_a), 4326)"
_SPLIT_PIECE_B_SOURCE = "ST_SetSRID(ST_GeomFromGeoJSON(:geojson_b), 4326)"


async def split_partition_metrics(
    db: AsyncSession,
    *,
    parent_version_id: uuid.UUID,
    piece_a_geojson: dict[str, Any],
    piece_b_geojson: dict[str, Any],
) -> dict[str, Decimal | None]:
    """The geometric partition test behind `service.split_contour`: whether
    two client-submitted pieces, normalised through the exact SAME pipeline
    `insert_version` itself stores a geometry through (`_normalized_geom_sql`),
    actually reconstruct the parent version's own geometry with no gap and no
    double-covered area.

    This never RE-DERIVES the cut: the adminka's `splitContour.ts` already
    owns that algorithm, tested six ways over a buffer/difference — PostGIS is
    asked to check the client's ANSWER here, never to recompute the question,
    so this module gains no second geometry-cutting engine that could quietly
    disagree with the front end's (`service.split_contour`'s own docstring
    explains the choice).

    Four areas come back, in m² over `geography` (`::numeric`, this module's
    own convention — a real `Decimal`, never a Python `float`): each piece on
    its own, their mutual intersection (near zero for two pieces that only
    share a border — ruling 15's "a shared border is a touch of zero area"
    applies here exactly as it does to `checks._overlap`), and the symmetric
    difference between their UNION and the parent's geometry (near zero only
    when the two pieces, together, cover the parent exactly — a gap between
    them and a piece straying outside the parent boundary both show up as the
    SAME non-zero number, because either failure leaves geometry on one side
    of the comparison that is not on the other).

    A piece whose normalised geometry collapses to nothing (a line, a point,
    an empty collection) reports `None` for its own area AND for both figures
    that need it (`intersection_m2`, `mismatch_m2`) — `service.split_contour`
    reads `None` as `piece_zero_area` without this function ever handing a
    NULL geometry to `ST_Area`/`ST_Intersection`/`ST_SymDifference`, every one
    of which would otherwise turn a NULL geometry into a NULL area instead of
    the number this contract promises for the cases where both pieces ARE
    real.
    """
    row = (
        (
            await db.execute(
                text(
                    "WITH pieces AS ("
                    "  SELECT"
                    f"    {_normalized_geom_sql(_SPLIT_PIECE_A_SOURCE)} AS a,"  # nosec B608
                    f"    {_normalized_geom_sql(_SPLIT_PIECE_B_SOURCE)} AS b"  # nosec B608
                    "), parent AS ("
                    "  SELECT geom AS g FROM contour_versions WHERE id = :parent_version_id"
                    ")"
                    " SELECT"
                    "   CASE WHEN pieces.a IS NULL OR ST_IsEmpty(pieces.a) THEN NULL"
                    "        ELSE ST_Area(pieces.a::geography)::numeric END AS area_a_m2,"
                    "   CASE WHEN pieces.b IS NULL OR ST_IsEmpty(pieces.b) THEN NULL"
                    "        ELSE ST_Area(pieces.b::geography)::numeric END AS area_b_m2,"
                    "   CASE WHEN pieces.a IS NULL OR pieces.b IS NULL"
                    "             OR ST_IsEmpty(pieces.a) OR ST_IsEmpty(pieces.b) THEN NULL"
                    "        ELSE ST_Area(ST_Intersection(pieces.a, pieces.b)::geography)::numeric"
                    "        END AS intersection_m2,"
                    "   CASE WHEN pieces.a IS NULL OR pieces.b IS NULL"
                    "             OR ST_IsEmpty(pieces.a) OR ST_IsEmpty(pieces.b) THEN NULL"
                    "        ELSE ST_Area(ST_SymDifference("
                    "               ST_Union(pieces.a, pieces.b), parent.g"
                    "             )::geography)::numeric"
                    "        END AS mismatch_m2"
                    " FROM pieces, parent"
                ),
                {
                    "parent_version_id": parent_version_id,
                    "geojson_a": json.dumps(piece_a_geojson),
                    "geojson_b": json.dumps(piece_b_geojson),
                },
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def list_versions(
    db: AsyncSession,
    contour_id: uuid.UUID,
    *,
    zone: Any,
    status: str | None,
    offset: int,
    limit: int,
) -> tuple[list[ContourVersion], int]:
    """Every version of ONE contour, oldest first — draft through archived —
    the discoverability gap `contour_card` (published only) never closed: a
    specialist's own draft and review submission, and the version a rahbar
    must approve, had no route at all (task defect 4a). `zone` is whatever
    `abac.zone_filter` built off `Organization.region_id`/`district_id` and
    `Contour.organization_id` — the SAME three columns `list_contours` checks
    — so a version outside the actor's own zone is invisible here exactly as
    a published contour outside it is invisible there; `status`, when given,
    narrows to one (`?status=review` is "awaiting my approval"). Geometry is
    never selected here (module docstring) — `version_detail` below is the
    only place one version's own shape crosses into Python."""
    conditions: list[Any] = [ContourVersion.contour_id == contour_id, zone]
    if status is not None:
        conditions.append(ContourVersion.status == status)
    joined = (
        select(ContourVersion.id)
        .join(Contour, Contour.id == ContourVersion.contour_id)
        .join(Organization, Organization.id == Contour.organization_id)
        .where(*conditions)
    )
    total = (await db.execute(select(func.count()).select_from(joined.subquery()))).scalar_one()
    rows = await db.execute(
        select(ContourVersion)
        .join(Contour, Contour.id == ContourVersion.contour_id)
        .join(Organization, Organization.id == Contour.organization_id)
        .where(*conditions)
        .order_by(ContourVersion.version_no)
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars().all()), total


async def version_detail(
    db: AsyncSession, contour_id: uuid.UUID, version_id: uuid.UUID, *, zone: Any
) -> Any | None:
    """One version's full detail, geometry included (`ST_AsGeoJSON`, computed
    in SQL — only the resulting STRING crosses into Python, module docstring)
    — the other half of defect 4a: `VersionOut` carries no geometry at all,
    so nothing could fetch one non-published version by id to actually look
    at it. `zone` is the SAME condition `list_versions` applies; a version
    outside it is indistinguishable from one that does not exist, matching
    `contour_card`'s own not-found shape."""
    result = await db.execute(
        select(
            ContourVersion.id,
            ContourVersion.contour_id,
            ContourVersion.version_no,
            ContourVersion.status,
            ContourVersion.source,
            ContourVersion.area_ha,
            ContourVersion.declared_area_ha,
            ContourVersion.accuracy_m,
            ContourVersion.survey_date,
            ContourVersion.effective_from,
            ContourVersion.approval_doc_id,
            ContourVersion.approved_by,
            ContourVersion.published_at,
            func.ST_AsGeoJSON(ContourVersion.geom).label("geometry"),
        )
        .join(Contour, Contour.id == ContourVersion.contour_id)
        .join(Organization, Organization.id == Contour.organization_id)
        .where(ContourVersion.id == version_id, ContourVersion.contour_id == contour_id, zone)
    )
    return result.one_or_none()


async def distance_to_published_version_m(
    db: AsyncSession, contour_id: uuid.UUID, *, lon: float, lat: float
) -> Decimal | None:
    """Metres from `(lon, lat)` to the contour's PUBLISHED version geometry, or
    `None` when there is none — the predicate runs entirely inside PostGIS
    (`ST_Distance` over `::geography`, so the great-circle distance is used
    rather than a planar approximation), never in Python (module convention:
    'gis.repo and gis.checks build SQL, PostGIS answers it'), the same
    `::geography` idiom `insert_version`'s own area computation and
    `checks._intersections` already use.

    `inspections` (level 5) is the caller (`gis.service.distance_to_contour_m`)
    — an inspector's GPS fix compared against the plot they are checking.
    `lon`/`lat` are bound values, never interpolated (this is a `text()` query,
    same reasoning as `checks._intersections`'s own nosec)."""
    meters = (
        await db.execute(
            text(
                "SELECT ST_Distance("
                "geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography"
                ") FROM contour_versions WHERE contour_id = :contour_id AND status = 'published'"
            ),
            {"lon": lon, "lat": lat, "contour_id": contour_id},
        )
    ).scalar_one_or_none()
    return None if meters is None else Decimal(str(meters))


async def contour_organization(db: AsyncSession, contour_id: uuid.UUID) -> uuid.UUID | None:
    """The leshoz a contour is filed under, or None if there is no such contour.
    Identity only — no version, no geometry."""
    rows = await db.execute(select(Contour.organization_id).where(Contour.id == contour_id))
    return rows.scalar_one_or_none()


def contour_organization_column(contour_id_col: Any) -> Any:
    """`contour_organization` above as a SQL EXPRESSION rather than a value: a
    correlated scalar subquery resolving whatever `contour_id_col` holds, row by
    row, to the organization that owns that contour.

    Exists for a caller that has to apply a zone rule inside a PAGED query and
    therefore cannot resolve one id at a time — `applications.repo.list_
    applications`, whose `assigned_org_id` is null until a reviewer takes the
    application into work, so the effective organization is
    `coalesce(assigned_org_id, <this>)`. Handed out through `gis.service`, never
    imported from here: the module boundary is about who may build SQL over
    `contours`, and this keeps that answer "gis" even when the surrounding
    SELECT belongs to somebody else.
    """
    return select(Contour.organization_id).where(Contour.id == contour_id_col).scalar_subquery()


async def contour_number(db: AsyncSession, contour_id: uuid.UUID) -> str | None:
    """The contour's own number, or None if there is no such contour. Identity
    only, exactly like `contour_organization` above."""
    rows = await db.execute(select(Contour.number).where(Contour.id == contour_id))
    return rows.scalar_one_or_none()


# --- Task 6: layer_features (restriction, protection, fire-ban and every
# other non-contour layer object) --------------------------------------------
#
# `layer_features.geom` is plain GEOMETRY, not MULTIPOLYGON: this catalogue
# also holds points (`water_points`) and lines (`cattle_corridors`), which
# `insert_version`'s own `ST_CollectionExtract(..., 3)` above would silently
# discard. The pipeline below stops one step earlier — force 2D, repair
# self-intersections, wrap as Multi* — and the RESULT's own type is validated
# by the caller (`gis.service.create_feature`) against the layer's declared
# `geometry_type` instead (task-6 brief's design note). Do not reuse
# `insert_version`'s expression unchanged for this table.

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


async def list_imports(
    db: AsyncSession, *, zone: Any, status: str | None, offset: int, limit: int
) -> tuple[list[GisImport], int]:
    """Every import batch, newest first — the same discoverability gap
    `list_versions` closes for contour versions (task defect 4a), one route
    smaller (task defect 4b): a batch awaiting `CONTOURS_APPROVE` had no route
    listing it at all, so the specialist who filed it and the rahbar who must
    approve it could only be handed its id out of band. `zone` is whatever
    `abac.zone_filter` built off `Organization.region_id`/`district_id` and
    `GisImport.organization_id` — the same three-axis check `list_contours`
    and `list_versions` apply, joined here even though `GisImport` already
    carries `organization_id` directly, because a region- or district-scoped
    actor still needs the join to `organizations` to be checked at all.
    `status`, when given, narrows to one (`?status=review` is "awaiting my
    approval")."""
    conditions: list[Any] = [zone]
    if status is not None:
        conditions.append(GisImport.status == status)
    joined = (
        select(GisImport.id)
        .join(Organization, Organization.id == GisImport.organization_id)
        .where(*conditions)
    )
    total = (await db.execute(select(func.count()).select_from(joined.subquery()))).scalar_one()
    rows = await db.execute(
        select(GisImport)
        .join(Organization, Organization.id == GisImport.organization_id)
        .where(*conditions)
        .order_by(GisImport.created_at.desc(), GisImport.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars().all()), total


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


async def import_version_statuses(db: AsyncSession, import_id: uuid.UUID) -> list[str]:
    """Every status one import's versions currently hold, regardless of which.
    `import_versions` above answers "which rows are at status X"; this answers
    "where has this batch got to as a whole", which is what tells an
    already-finished batch from one that never had any versions at all."""
    rows = await db.execute(
        select(ContourVersion.status).where(ContourVersion.import_id == import_id)
    )
    return list(rows.scalars().all())


# --- Task 8: the read API for 3.7/3.9 -----------------------------------------

# The hard ceiling on ONE `GET /gis/layers/{code}/features` response. This
# endpoint answers a GeoJSON document, not a page: a map client asks for a
# viewport and a `?bbox=` is the intended narrowing, so paging it would mean
# inventing page semantics for something no map consumer paginates. Unbounded,
# though, `GET /gis/layers/forest_fund/features` with no bbox serialises the
# WHOLE fund boundary — hundreds of polygons the Agency has yet to deliver in
# full — into one document. Hence a cap plus an explicit `truncated` flag in
# the response (a foreign member, legal in RFC 7946), so a client can never
# mistake a clipped collection for the whole layer; the fix is to pass a bbox,
# and the flag is what tells them to.
FEATURE_COLLECTION_LIMIT = 2000


async def list_contours(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID | None,
    bbox: tuple[float, float, float, float] | None,
    zone: Any,
    offset: int,
    limit: int,
) -> tuple[list[Any], int]:
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
    selected into Python (module docstring).

    Paged (`offset`/`limit`, `PageParams`' own numbers) and returned with the
    total, per design/03's `?page=1&page_size=20` convention. Unbounded, this
    answered every published contour in the country to any authenticated
    caller — ~13,500 rows once the leshozes land, and an applicant picking a
    plot is exactly who reaches it."""
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
    joined = (
        select(Contour.id)
        .join(ContourVersion, ContourVersion.contour_id == Contour.id)
        .join(Organization, Organization.id == Contour.organization_id)
        .where(*conditions)
    )
    total = (await db.execute(select(func.count()).select_from(joined.subquery()))).scalar_one()
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
        .offset(offset)
        .limit(limit)
    )
    return list(result.all()), total


async def contour_features_geojson(
    db: AsyncSession,
    *,
    bbox: tuple[float, float, float, float] | None,
    zone: Any,
    organization_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Published contours as a GeoJSON FeatureCollection — the layer a map
    draws when NOTHING is picked yet, so an applicant can see the leshoz's
    parcels at once instead of finding them one at a time in the list.

    Deliberately a sibling of `list_contours` rather than a flag on it: that
    one is PAGED (`?page=&page_size=`, max 100) because it feeds a list, and
    paging is the wrong shape for a map, which wants everything inside the
    viewport and nothing outside it. Same predicates though — published
    versions only (decision 6), the same `zone` filter, the same
    `ST_Intersects` bbox — so the two can never disagree about which contours
    a caller may see.

    Capped like `features_geojson`, with `truncated` saying so: without a
    bbox this is every published contour in the country, ~13,500 rows once the
    leshozes land, and the flag is what tells a client to send a viewport
    instead of trusting a clipped answer.

    Properties stay to identity and area on purpose. Occupancy costs a
    per-contour aggregate over permits (`contour_card`'s own provider seam),
    and a map that draws 2,000 polygons would pay it 2,000 times for figures
    only the picked one ever shows.

    Two conditions decision #178 adds, both load-bearing for the SAME reason
    (`ST_AsGeoJSON(NULL)` is NULL, and `json.loads(None)` raises — never a row
    this collection may return): `ContourVersion.geom.is_not(None)` drops any
    contour filed by requisites alone, and `Organization.gis_enabled.is_(True)`
    drops every contour of an organization the switch turns off, REGARDLESS of
    whether that contour happens to carry geometry — the flag is the
    authoritative "does this leshoz show a map" answer, not an inference from
    what any one row happens to have on it today.
    """
    conditions: list[Any] = [
        ContourVersion.status == "published",
        ContourVersion.geom.is_not(None),
        Organization.gis_enabled.is_(True),
        zone,
    ]
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
    rows = (
        await db.execute(
            select(
                Contour.id.label("contour_id"),
                Contour.number,
                Contour.organization_id,
                ContourVersion.area_ha,
                func.ST_AsGeoJSON(ContourVersion.geom).label("geometry"),
            )
            .join(ContourVersion, ContourVersion.contour_id == Contour.id)
            .join(Organization, Organization.id == Contour.organization_id)
            .where(*conditions)
            .order_by(Contour.number)
            # One past the cap, so "there are more" is read off this query
            # rather than a second COUNT over the same predicate.
            .limit(FEATURE_COLLECTION_LIMIT + 1)
        )
    ).all()
    truncated = len(rows) > FEATURE_COLLECTION_LIMIT
    rows = rows[:FEATURE_COLLECTION_LIMIT]
    return {
        "type": "FeatureCollection",
        "truncated": truncated,
        "features": [
            {
                "type": "Feature",
                "id": str(row.contour_id),
                "geometry": json.loads(row.geometry),
                "properties": {
                    "contour_id": str(row.contour_id),
                    "number": row.number,
                    "organization_id": str(row.organization_id),
                    "area_ha": str(row.area_ha),
                },
            }
            for row in rows
        ],
    }


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
    status: str = "published",
    import_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """The layer's features at one status as a GeoJSON FeatureCollection, built
    entirely in SQL (`ST_AsGeoJSON` per row, decision 6): geometry crosses
    into Python only as the string PostGIS already rendered, never as a value
    this module reconstructs itself (module docstring). `valid_on`, when
    given, keeps only features whose validity period (if any) contains that
    date — the same window `checks._RESTRICTIONS_CANDIDATES_SQL` gates
    `fire_bans` on, generalised here to every feature since most carry no
    period at all (`valid_from`/`valid_to` both NULL means "always valid").

    `status` was hard-coded to `published` until the final review of 3.6a,
    which left an imported non-contour batch's `draft` rows unreachable: the
    batch endpoints refuse a non-contour batch, the per-feature publish route
    needs an id, and no endpoint returned those ids. That mattered most for
    `forest_fund` — `checks._within_fund` stays `skipped` until that layer has
    published features, so the stage's own gating check could not be switched
    on through its own API. `gis.service.list_features` gates any non-published
    status behind `gis.layers.manage`; an applicant never sees a draft.
    """
    conditions: list[Any] = [GisLayer.code == layer_code, LayerFeature.status == status]
    if import_id is not None:
        conditions.append(LayerFeature.import_id == import_id)
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
            # One past the cap, so "there are more" is a fact read off the
            # query rather than a second COUNT over the same predicate.
            .limit(FEATURE_COLLECTION_LIMIT + 1)
        )
    ).all()
    truncated = len(rows) > FEATURE_COLLECTION_LIMIT
    rows = rows[:FEATURE_COLLECTION_LIMIT]
    return {
        "type": "FeatureCollection",
        "truncated": truncated,
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


# --- Stage 3.7: what norms.checks needs (ruling 15) --------------------------


async def features_intersecting(
    db: AsyncSession,
    contour_id: uuid.UUID,
    layer_codes: Sequence[str],
    period_from: date,
    period_to: date,
) -> list[Any]:
    """Published features of the named layers that both overlap the contour's
    published geometry by more than the area tolerance AND are valid during the
    requested period. Area, not touch: two neighbours share a border and
    `ST_Intersects` alone calls that an overlap (lesson).

    A feature with no validity dates is always valid — an open-ended protection
    zone is the normal case; a fire ban is the one that carries a period."""
    tolerance = await settings_store.get_int(db, "gis_overlap_tolerance_m2")
    rows = await db.execute(
        text(
            "SELECT f.id, l.code AS layer_code, f.name, f.valid_from, f.valid_to, "
            "       ST_Area(ST_Intersection(f.geom, v.geom)::geography) AS area_m2 "
            "FROM layer_features f "
            "JOIN gis_layers l ON l.id = f.layer_id "
            "JOIN contour_versions v ON v.contour_id = :contour AND v.status = 'published' "
            "WHERE l.code = ANY(:codes) AND f.status = 'published' "
            "  AND ST_Intersects(f.geom, v.geom) "
            "  AND ST_Area(ST_Intersection(f.geom, v.geom)::geography) > :tolerance "
            "  AND (f.valid_from IS NULL OR f.valid_from <= :period_to) "
            "  AND (f.valid_to IS NULL OR f.valid_to >= :period_from)"
        ).bindparams(
            contour=contour_id,
            codes=list(layer_codes),
            tolerance=tolerance,
            period_from=period_from,
            period_to=period_to,
        )
    )
    return list(rows.all())
