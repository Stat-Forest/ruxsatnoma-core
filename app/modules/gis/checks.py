"""Server-side topology checks (tz/07, decision #24). Every predicate runs in
PostGIS; this module only decides what a result MEANS.

Blocking vs advisory (ruling 16): an invalid geometry, a plot outside the forest
fund and a real overlap with another published contour block publication — they
are data defects. An intersection with a restriction, a protection zone or a fire
ban is a WARNING: grazing there is limited, not impossible, and the decision
belongs to the norm (3.7) and the application review (3.9), which read these very
rows. Adding a layer to BLOCKING is a product decision, not a refactor.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any, TypedDict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.time import business_today
from app.modules.gis.models import RESTRICTION_LAYER_CODES


class CheckResult(TypedDict):
    check: str
    result: str  # pass | fail | warning | skipped
    details: dict[str, Any]


BLOCKING = frozenset({"validity", "within_fund", "overlap"})


def is_blocked(results: list[CheckResult]) -> bool:
    return any(r["check"] in BLOCKING and r["result"] == "fail" for r in results)


def jsonable(value: Any) -> Any:
    """Recursively converts a `CheckResult`-shaped structure (or any value
    nested inside one) into something a plain `json.dumps` can serialize:
    `Decimal` -> `float`, `uuid.UUID` -> `str`, recursing through `dict`/
    `list`, everything else passed through unchanged.

    `CheckResult` has two callers that need this, for DIFFERENT reasons —
    and the UUID branch is NOT optional for either of them, even though only
    ONE of the two would actually break without it:

    - `gis.service.publish_version`'s `ERR-GIS-003` details reach the wire
      through `app.main`'s `DomainError` handler, which renders the response
      with Starlette's `JSONResponse` — stock `json.dumps`, NO encoder at
      all (confirmed by reading Starlette's own `render()`). `_intersections`'
      `overlap`/`restrictions` results carry a raw `Decimal` (`area_m2`) AND
      a raw `uuid.UUID` (`feature_id`); either one reaching `json.dumps`
      unconverted raises `TypeError` INSIDE the exception handler itself,
      turning a clean 422 into a 500 — reproduced for real by temporarily
      dropping this call and watching
      `test_publishing_an_overlapping_version_is_refused_with_the_check_report`
      fail with exactly that traceback.
    - `gis.schemas.CheckResultOut.details` (typed `dict[str, Any]`) reaches
      the wire through pydantic's own response-model serializer instead.
      Pydantic's "infer the type at runtime" encoding for a bare value
      nested under `Any` already renders a `uuid.UUID` correctly on its own
      — so the UUID branch happens to be a no-op on THIS path — but still
      renders a `Decimal` as a quoted STRING instead of a JSON number, wrong
      for `area_m2` regardless of which path is asking.

    Do not read the schemas path's tolerance for a missing UUID branch as
    evidence the branch is optional: that tolerance is a property of
    pydantic's encoder, not of this data. This used to be two separate,
    near-identical local copies — one in `service.py`, one in `schemas.py`
    — that had ALREADY diverged exactly this way (the schemas copy never
    grew a UUID branch, quietly relying on pydantic to cover for it) before
    a review caught it. Unified here, in the module that defines
    `CheckResult` itself, so there is only one place to update when
    `details` grows a new kind of non-primitive value.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    return value


async def run_checks(
    db: AsyncSession, *, version_id: uuid.UUID, on_date: date | None = None
) -> list[CheckResult]:
    on_date = on_date or business_today()
    tolerance = await settings_store.get_int(db, "gis_overlap_tolerance_m2")
    return [
        await _validity(db, version_id),
        await _within_fund(db, version_id),
        await _restrictions(db, version_id, on_date, tolerance),
        await _overlap(db, version_id, tolerance),
    ]


async def _validity(db: AsyncSession, version_id: uuid.UUID) -> CheckResult:
    """Normalisation (`repo.insert_version`'s own `ST_MakeValid` pipeline)
    already repairs self-intersections on the way in — a failure here means the
    repair itself produced something invalid, not that the source data was
    messy."""
    valid = await db.scalar(
        text("SELECT ST_IsValid(geom) FROM contour_versions WHERE id = :vid"),
        {"vid": version_id},
    )
    if valid:
        return {"check": "validity", "result": "pass", "details": {}}
    return {"check": "validity", "result": "fail", "details": {"reason": "invalid_geometry"}}


async def _within_fund(db: AsyncSession, version_id: uuid.UUID) -> CheckResult:
    """The degradation of ruling 9, in one round trip: `forest_fund` has no
    published features at all today (the Agency has not delivered the real
    boundary yet, П.7) — that is `skipped`, not a false `fail` against an empty
    layer. Once the layer is non-empty, a plot outside every published boundary
    is a genuine data defect.

    The fund boundary arrives as many polygons (151 for Burchmulla alone), so
    "inside" means inside their UNION, not inside any single one of them — a
    contour that straddles the seam between two adjoining fund polygons is
    inside neither individually but is still legitimately within the fund
    (task-4 review, finding 1; a `bool_or(ST_Within(v.geom, f.geom))` against
    each row separately reported that seam as `outside_forest_fund`). Union
    only the features that actually intersect the contour, so the GIST index
    still narrows the candidate set instead of unioning the whole layer on
    every check. `COALESCE(..., v.geom)` is load-bearing: when nothing
    intersects at all, `ST_Union` over zero rows is NULL and
    `ST_Difference(geom, NULL)` is NULL too — reading that as "nothing lies
    outside" would wrongly pass a contour nowhere near the fund.
    """
    row = (
        await db.execute(
            text(
                "WITH fund AS ("
                "  SELECT f.geom FROM layer_features f"
                "  JOIN gis_layers l ON l.id = f.layer_id"
                "  WHERE l.code = 'forest_fund' AND f.status = 'published'"
                ")"
                " SELECT (SELECT count(*) FROM fund) AS fund_features,"
                "        NOT ST_IsEmpty(COALESCE(ST_Difference(v.geom, ("
                "          SELECT ST_Union(fund.geom) FROM fund"
                "          WHERE ST_Intersects(fund.geom, v.geom)"
                "        )), v.geom)) AS outside"
                " FROM contour_versions v WHERE v.id = :vid"
            ),
            {"vid": version_id},
        )
    ).one()
    if row.fund_features == 0:
        return {"check": "within_fund", "result": "skipped", "details": {"reason": "layer_empty"}}
    if row.outside:
        return {
            "check": "within_fund",
            "result": "fail",
            "details": {"reason": "outside_forest_fund"},
        }
    return {"check": "within_fund", "result": "pass", "details": {}}


async def _intersections(
    db: AsyncSession,
    version_id: uuid.UUID,
    *,
    candidates_sql: str,
    params: dict[str, Any],
    tolerance: int,
) -> list[dict[str, Any]]:
    """The shape shared by `_restrictions` and `_overlap` (decision 6 of the task-4
    brief): join the version's geometry against a candidate set via
    `ST_Intersects` (cheap — uses each side's spatial index), compute the actual
    intersection area exactly once per candidate, and keep only pairs that clear
    `tolerance` — a shared border is a touch of zero area, not an overlap
    (ruling 15). `candidates_sql` selects `(layer, feature_id, name, geom)` rows
    to test against; it is always a fixed, module-local SQL string defined below
    (never caller/request input), so embedding it by plain string formatting
    carries no injection risk — the only values that ever cross the wire as data
    are bound through `params`.
    """
    rows = (
        await db.execute(
            text(
                "SELECT t.layer, t.feature_id, t.name, t.area_m2 FROM ("
                "  SELECT c.layer, c.feature_id, c.name,"
                "         ST_Area(ST_Intersection(v.geom, c.geom)::geography)::numeric AS area_m2"
                # Bandit flags this as B608 (string-built SQL) on the pattern
                # alone; candidates_sql is always one of the two fixed constants
                # below, never caller input, and every actual value is bound
                # through `params` below, not this string — see the docstring.
                f"  FROM contour_versions v, ({candidates_sql}) AS c"  # nosec B608
                "  WHERE v.id = :version_id AND ST_Intersects(v.geom, c.geom)"
                ") t"
                " WHERE t.area_m2 > :tolerance"
                " ORDER BY t.area_m2 DESC"
            ),
            {"version_id": version_id, "tolerance": tolerance, **params},
        )
    ).all()
    return [
        {
            "layer": row.layer,
            "feature_id": row.feature_id,
            "name": row.name,
            # Decimal, not float (project rule: areas are numeric) — the SQL
            # casts ::numeric above, so this is asyncpg's own Decimal, not a
            # Python float() conversion (task-4 review, finding 2). The one
            # HTTP response that returns this value has its own serializer
            # (schemas.CheckResultOut) so the JSON stays a number, not a
            # stringified Decimal.
            "area_m2": row.area_m2,
        }
        for row in rows
    ]


# layer_features candidates for `_restrictions`: the three layers of ruling 15,
# published only, a fire ban additionally gated to periods containing `on_date`
# (a fire ban is a period plus a territory — last year's ban restricts nothing).
_RESTRICTIONS_CANDIDATES_SQL = (
    "SELECT l.code AS layer, f.id AS feature_id, f.name AS name, f.geom AS geom"
    " FROM layer_features f JOIN gis_layers l ON l.id = f.layer_id"
    " WHERE l.code = ANY(:layer_codes) AND f.status = 'published'"
    "   AND (l.code <> 'fire_bans' OR ("
    "     (f.valid_from IS NULL OR f.valid_from <= :on_date) AND"
    "     (f.valid_to IS NULL OR f.valid_to >= :on_date)))"
)


async def _restrictions(
    db: AsyncSession, version_id: uuid.UUID, on_date: date, tolerance: int
) -> CheckResult:
    """Never `fail` (ruling 16) — grazing under a restriction/protection/fire-ban
    layer is limited, not impossible; the norm (3.7) and application review (3.9)
    decide, reading these same rows."""
    items = await _intersections(
        db,
        version_id,
        candidates_sql=_RESTRICTIONS_CANDIDATES_SQL,
        params={"layer_codes": list(RESTRICTION_LAYER_CODES), "on_date": on_date},
        tolerance=tolerance,
    )
    if items:
        return {"check": "restrictions", "result": "warning", "details": {"items": items}}
    return {"check": "restrictions", "result": "pass", "details": {}}


# The candidate set is every OTHER contour's published geometry — a version
# never overlaps its own contour's published version just because it now has
# two versions on record (excluded via the contour_id <> subquery).
_OVERLAP_CANDIDATES_SQL = (
    "SELECT 'contours' AS layer, ov.id AS feature_id, oc.number AS name, ov.geom AS geom"
    " FROM contour_versions ov JOIN contours oc ON oc.id = ov.contour_id"
    " WHERE ov.status = 'published'"
    "   AND ov.contour_id <> (SELECT contour_id FROM contour_versions WHERE id = :version_id)"
)


async def _overlap(db: AsyncSession, version_id: uuid.UUID, tolerance: int) -> CheckResult:
    """This is NOT tz/07's "intersection with active permits (double booking)"
    row — that check is on an APPLICATION (period + activity + applicant) and
    belongs to 3.9. This one compares a version against the published geometry
    of other contours: a data defect, blocking publication (ruling 16)."""
    items = await _intersections(
        db, version_id, candidates_sql=_OVERLAP_CANDIDATES_SQL, params={}, tolerance=tolerance
    )
    if items:
        return {"check": "overlap", "result": "fail", "details": {"items": items}}
    return {"check": "overlap", "result": "pass", "details": {}}
