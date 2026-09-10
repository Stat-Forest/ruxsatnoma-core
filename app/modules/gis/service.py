"""Business rules of the spatial core. Public surface for levels 3+ (norms 3.7,
applications 3.9): published_version(), list_contours(), run_checks()."""

import json
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.abac import Zone, zone_filter, zone_of
from app.core.errors import DomainError, err
from app.core.models import MediaFile
from app.core.schemas import PageParams
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import Organization
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth.deps import SUPERUSER_ROLE
from app.modules.auth.models import User
from app.modules.gis import checks, repo
from app.modules.gis.models import (
    CONTOUR_LAYER_CODE,
    Contour,
    ContourVersion,
    GisImport,
    GisLayer,
    LayerFeature,
)
from app.modules.gis.permissions import LAYERS_MANAGE


def _json_safe(value: Any) -> Any:
    """Audit snapshots go through `audit_log`'s JSONB columns via the stock
    `json.dumps` (no encoder configured) — the lesson "A JSONB column fed by the
    stock json.dumps rejects Decimal and date" applies to every old_value/
    new_value this module writes, since version metadata is exactly Decimal
    (area/accuracy) and date (survey_date/effective_from). Mirrors
    `admin.users_service._json_safe` — kept local rather than shared, same as
    that module's own copy."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


async def list_layers(db: AsyncSession) -> list[GisLayer]:
    """A pass-through today (task-2 review, finding 2): this read acquires a real
    rule inside this same stage — Task 8 must refuse a non-public layer's
    features to an applicant — and that rule belongs here, not in the router."""
    return await repo.list_layers(db)


async def update_layer(
    db: AsyncSession,
    code: str,
    *,
    actor: User,
    style: dict[str, Any] | None = None,
    is_public: bool | None = None,
    status: str | None = None,
) -> GisLayer:
    layer = await repo.layer_by_code(db, code)
    if layer is None:
        raise err("ERR-SYS-003")
    before = {"style": layer.style, "is_public": layer.is_public, "status": layer.status}
    if style is not None:
        layer.style = style
    if is_public is not None:
        layer.is_public = is_public
    if status is not None:
        layer.status = status
    await db.flush()
    # An in-place UPDATE leaves onupdate columns expired (lesson) — the response
    # serializes this row, so refresh before returning it.
    await db.refresh(layer)
    await audit.log(
        db,
        action="gis_layer.update",
        user_id=actor.id,
        object_type="gis_layer",
        object_id=layer.id,
        old_value=before,
        new_value={"style": layer.style, "is_public": layer.is_public, "status": layer.status},
    )
    return layer


def _organization_in_zone(zone: Zone, org: Organization) -> bool:
    """Per-row equivalent of `zone_filter`'s SQL for ONE organization row —
    the same semantics `admin.users_service._within_zone` applies to a `User`,
    applied here to the organization a gis object hangs off: an axis the zone
    leaves `None` is unrestricted, a set axis must match exactly."""
    if zone.region_id is not None and zone.region_id != org.region_id:
        return False
    if zone.district_id is not None and zone.district_id != org.district_id:
        return False
    if zone.organization_id is not None and zone.organization_id != org.id:
        return False
    return True


async def _assert_in_zone(db: AsyncSession, actor: User, organization_id: uuid.UUID) -> None:
    """Per-request zone check on ALL THREE axes of `app/core/abac.py`'s `Zone`.
    Separate from the `CONTOURS_MANAGE`/`CONTOURS_APPROVE` permission check the
    router already applies (lesson: "Zone scoping is not a permission check — a
    read path needs both", and here every write path needs both too): it
    answers WHOSE organization, not WHETHER the actor may manage contours at
    all. Takes the organization id rather than a `Contour` row — `create_contour`
    has no row yet when it needs this check, since `organization_id` comes
    straight from the request body.

    `Contour`/`GisImport` carry only `organization_id`, so the region and
    district axes are resolved by loading the ORGANIZATION and comparing its
    own `region_id`/`district_id` — exactly what `repo.list_contours`' JOIN to
    `organizations` already does for the read path. Comparing
    `zone.organization_id` alone (what this did until the final review of
    3.6a) let an actor with a region — or district — but NO organization of
    their own pass for every organization in the country: that shape is
    creatable today (`admin.users_service.create_user` sets the three columns
    independently) and migration 0010 grants `gis.contours.approve` to
    `leadership`/`chief_forester`, so an oblast-level chief forester could
    approve and publish contour versions for any leshoz nationwide.

    An organization that does not exist fails the check for a zoned actor:
    nothing can prove it is inside their zone. A zone empty on every axis is
    republic-wide and short-circuits before the read, so the common case costs
    no query at all.

    Read through `admin.repo` — reference data is never re-queried from another
    module's repo (CLAUDE.md); `db.get`'s identity map makes a repeated lookup
    inside one request free.
    """
    zone = zone_of(actor)
    if zone == Zone(None, None, None):
        return
    org = await admin_repo.get_organization(db, organization_id)
    if org is None or not _organization_in_zone(zone, org):
        raise err("ERR-ACL-001")


async def _assert_parent(
    db: AsyncSession,
    parent_id: uuid.UUID,
    *,
    organization_id: uuid.UUID,
    child_id: uuid.UUID | None = None,
) -> None:
    """A sub-contour's parent must exist and belong to the SAME organization —
    `contours.parent_id` is a plain FK with no organization condition of its
    own, so nothing below this stops a leshoz from hanging its sub-contour off
    another leshoz's row. Shared by `create_contour` and `update_contour`
    (decision #49 ruling 11: the importer creates everything flat and the
    hierarchy is set afterwards through `PATCH /gis/contours/{id}`), so the two
    cannot drift apart."""
    if child_id is not None and parent_id == child_id:
        raise err("ERR-VAL-001", details={"reason": "parent_is_self"})
    parent = await repo.contour_by_id(db, parent_id)
    if parent is None:
        raise err("ERR-VAL-001", details={"reason": "parent_not_found"})
    if parent.organization_id != organization_id:
        raise err("ERR-VAL-001", details={"reason": "parent_other_organization"})


async def create_contour(
    db: AsyncSession,
    *,
    layer_id: uuid.UUID,
    organization_id: uuid.UUID,
    number: str,
    kind: str = "contour",
    parent_id: uuid.UUID | None = None,
    actor: User,
) -> Contour:
    """201 identity row (design/03: `POST /gis/contours`).

    The `parent_id`/`kind` relationship (`parent_needs_subcontour` CHECK) is
    cheap and non-racy to validate in Python — both fields come from this SAME
    request, no concurrent actor can change what THIS caller intended — so it is
    pre-checked here, the same way `admin.service.add_classifier_item`
    pre-checks `valid_to`/`valid_from` rather than let a DB CHECK surface as an
    unhandled 500. The number's uniqueness, by contrast, genuinely races against
    other concurrent creates: that one is caught from the flush, never
    pre-checked.

    `organization_id` is zone-checked before anything else: migration 0010
    grants `gis.contours.manage` to `gis_specialist`, and a leshoz-level
    specialist has their own `organization_id` set, so without this an actor
    scoped to one leshoz could create a contour under a different one — the
    same check `update_contour`/`create_version`/`update_version` already apply
    to an existing row, applied here to the request's own value before any row
    exists (final review, finding 1).

    Every reference the request supplies — `layer_id`, `organization_id`,
    `parent_id` — is resolved BEFORE the insert, so the one `IntegrityError`
    left to catch is the unique violation it names. `except IntegrityError`
    also catches an FK violation, and this function used to report all four
    failures as `409 ERR-GIS-005 {"reason": "number_taken"}`: a nonexistent
    layer answered "that number is taken". Nothing checked that the layer WAS
    the contours layer either, so a contour could be created under
    `water_points` and would still appear in `list_contours`, which filters by
    no layer at all. `create_feature` already reasons this way (layer resolved
    first, the residual `IntegrityError` meaning the one FK the caller
    supplies); this now matches it.
    """
    await _assert_in_zone(db, actor, organization_id)
    layer = await repo.layer_by_id(db, layer_id)
    if layer is None:
        raise err("ERR-SYS-003")
    if layer.code != CONTOUR_LAYER_CODE:
        raise err("ERR-VAL-001", details={"reason": "not_the_contours_layer"})
    if await admin_repo.get_organization(db, organization_id) is None:
        raise err("ERR-VAL-001", details={"reason": "organization_not_found"})
    if parent_id is not None:
        if kind != "subcontour":
            raise err("ERR-VAL-001", details={"reason": "parent_needs_subcontour"})
        await _assert_parent(db, parent_id, organization_id=organization_id)
    contour = Contour(
        layer_id=layer_id,
        organization_id=organization_id,
        number=number,
        kind=kind,
        parent_id=parent_id,
        created_by=actor.id,
    )
    db.add(contour)
    try:
        await db.flush()
    except IntegrityError as exc:
        # `uq_contour_number`, the one constraint left that a pre-check
        # cannot own: the number's uniqueness genuinely races against other
        # concurrent creates. The session is poisoned after this (same
        # reasoning as create_version's DBAPIError below) — raise immediately,
        # touch `db` no further on this path; get_db's rollback-on-exception
        # clears the aborted transaction.
        raise err("ERR-GIS-005", details={"reason": "number_taken"}) from exc
    await audit.log(
        db,
        action="contour.create",
        user_id=actor.id,
        object_type="contour",
        object_id=contour.id,
        new_value={
            "layer_id": str(layer_id),
            "organization_id": str(organization_id),
            "number": number,
            "kind": kind,
            "parent_id": str(parent_id) if parent_id is not None else None,
        },
    )
    return contour


async def update_contour(
    db: AsyncSession, contour_id: uuid.UUID, *, actor: User, **fields: Any
) -> Contour:
    """`PATCH /gis/contours/{id}` — identity-level housekeeping: archiving, and
    the contour/sub-contour HIERARCHY. Geometry changes always go through a new
    version, never through this route.

    `kind`/`parent_id` are settable here because decision #49 ruling 11 says so:
    the importer creates everything flat BECAUSE it refuses to guess a
    hierarchy from the file, and this edit is what it assumed would exist.
    `ContourPatch` carried `status` alone until the final review of 3.6a, so an
    imported contour could never become a sub-contour at all — and stage 7's
    data loading depends on it.

    The `parent_needs_subcontour` pairing is checked against the row as it
    WILL BE, not against the request alone: patching `parent_id` onto a row
    that is still `kind='contour'`, or clearing `kind` back to `contour` while
    a parent remains, are both the same violation from opposite directions, and
    the DB CHECK would otherwise surface as an unhandled 500. Only fields the
    request actually supplied are read (`exclude_unset` at the router), so
    `{"status": "archived"}` never disturbs the hierarchy.
    """
    contour = await repo.contour_by_id(db, contour_id)
    if contour is None:
        raise err("ERR-SYS-003")
    await _assert_in_zone(db, actor, contour.organization_id)
    kind = fields.get("kind", contour.kind)
    parent_id = fields.get("parent_id", contour.parent_id)
    if parent_id is not None:
        if kind != "subcontour":
            raise err("ERR-VAL-001", details={"reason": "parent_needs_subcontour"})
        if parent_id != contour.parent_id:
            await _assert_parent(
                db, parent_id, organization_id=contour.organization_id, child_id=contour.id
            )
    before = {key: _json_safe(getattr(contour, key)) for key in fields}
    for key, value in fields.items():
        setattr(contour, key, value)
    await db.flush()
    # An in-place UPDATE leaves onupdate columns expired, not refreshed (lesson).
    await db.refresh(contour)
    await audit.log(
        db,
        action="contour.update",
        user_id=actor.id,
        object_type="contour",
        object_id=contour.id,
        old_value=before,
        new_value={key: _json_safe(getattr(contour, key)) for key in fields},
    )
    return contour


def _assert_geometry_or_declared_area(fields: Mapping[str, Any]) -> None:
    """Decision #178: a version with neither geometry nor a declared area has
    nothing to make `area_ha` (NOT NULL, > 0) out of. Pre-checked here, the
    same way `create_contour` pre-checks its own same-request fields, so this
    never reaches the DB CHECK (`geom_or_declared_area`) as an `IntegrityError`
    — which `create_version`'s own `except IntegrityError` below would
    misreport as `version_conflict`, the one collision that clause actually
    owns."""
    if fields.get("geojson") is not None or fields.get("wkb") is not None:
        return
    declared = fields.get("declared_area_ha")
    if declared is None or declared <= 0:
        raise err("ERR-VAL-001", details={"reason": "geometry_or_declared_area_required"})


async def create_version(
    db: AsyncSession, contour_id: uuid.UUID, *, actor: Any, **fields: Any
) -> ContourVersion:
    contour = await repo.contour_by_id(db, contour_id)
    if contour is None:
        raise err("ERR-SYS-003")
    await _assert_in_zone(db, actor, contour.organization_id)
    _assert_geometry_or_declared_area(fields)
    version_no = await repo.next_version_no(db, contour_id)
    try:
        version = await repo.insert_version(
            db, contour_id=contour_id, version_no=version_no, created_by=actor.id, **fields
        )
    except IntegrityError as exc:
        # `IntegrityError` IS a `DBAPIError` subclass, so it must be caught (and
        # distinguished) BEFORE the broader `except DBAPIError` below — Python
        # tries `except` clauses in order, and this one only matches PostgreSQL
        # error class 23 (integrity constraint violation). `next_version_no` is
        # an unlocked `SELECT MAX(version_no)+1`: two concurrent creates on one
        # contour can both compute the same number and collide on
        # `uq_contour_version_no`. Without this clause that collision fell
        # through to the branch below and was reported as "unreadable
        # geometry" — a data-format error — for what is actually a concurrency
        # conflict; Task 7's bulk import drives this same path, so the
        # confusion would recur under real load (final review, finding 3).
        raise err("ERR-GIS-005", details={"reason": "version_conflict"}) from exc
    except DBAPIError as exc:
        # Everything else reaching here is a genuine PostGIS parse failure
        # (`ST_GeomFromGeoJSON`'s malformed-input raise is SQLSTATE class XX,
        # `sqlalchemy.exc.InternalError` — verified NOT an `IntegrityError`, so
        # it never matches the clause above). It poisons the session the same
        # way an IntegrityError does — raise immediately and nothing else
        # touches `db` on this path; get_db's except-and-rollback
        # (app/core/deps.py) is what actually clears the aborted transaction
        # before this becomes a response. Do not add an audit call here: it
        # would run inside the still-aborted transaction and fail a second
        # time.
        raise err("ERR-GIS-001", details={"reason": "unreadable_geometry"}) from exc
    await audit.log(
        db,
        action="contour_version.create",
        user_id=actor.id,
        object_type="contour_version",
        object_id=version.id,
        new_value={"contour_id": str(contour_id), "version_no": version_no},
    )
    return version


# --- Split: one parent contour into two subcontours (decision #91) ----------
#
# `POST /gis/contours/{parent_id}/split`, `CONTOURS_MANAGE` (the same
# permission `create_contour`/`create_version` require — the specialist draws
# a split themselves; the two resulting drafts are approved later through the
# ordinary lifecycle, same as any other new version). Replaces the adminka's
# own client-composed `createContour` + `createVersion`, twice, which decision
# #91's own text names as "not atomic": a failure between the two calls left
# one new contour and no second, with nothing telling the operator which half
# actually happened.
#
# **The parent's own row and its published version are untouched by this
# call.** Decision #91 chose "the parent stays" over "replace the parent's
# geometry with two new contours" precisely so a permit issued before the
# split keeps pointing at something that still exists and can be read; this
# function does not go further and archive or edit that published version
# either — it stays published, at its own area and geometry, exactly as
# `create_contour` left every other contour it never touches. Two
# consequences follow from leaving it alone rather than superseding it:
#
#   1. `ERR-GIS-005 active_permit_on_parent` below refuses a split while the
#      occupancy seam reports ANY active permit against the parent, so by the
#      time a split succeeds nothing live still depends on the parent's
#      geometry meaning "the whole area" — only closed history does, and
#      closed history is exactly what decision #91 says must keep reading.
#   2. The two new subcontours' geometry sits entirely INSIDE the parent's
#      still-published one, so `checks._overlap` (parent vs. child, both
#      published) WOULD fail if a specialist tried to publish a child version
#      without first archiving the parent's own — `archive_version`, already
#      on this router, is how that is done today, deliberately a SEPARATE,
#      auditable act by the rahbar (`CONTOURS_APPROVE`) rather than something
#      this call does silently on the specialist's behalf. Documented here,
#      not fixed here: retiring the parent's published version the moment a
#      split is confirmed useful would also make undoing a split (decision
#      #91's own "an undo is deleting two children") no longer a plain
#      delete, since the parent's version would need un-archiving too. Left
#      for whoever wires the adminka's own split screen to decide, informed
#      by how operators actually use this endpoint.
#
# Every one of the four checks below is a distinct, named refusal (task
# brief): `already_split`/`parent_not_published`/`active_permit_on_parent`
# share `ERR-GIS-005` (409) — a conflict with the PARENT's own current state,
# the exact category that code already covers for `create_contour`/
# `create_version`/the lifecycle actions; the geometry-partition failures
# below share the new `ERR-GIS-006` (422) — a defect in the SUBMITTED
# GEOMETRY, not a state conflict, mirroring how `ERR-GIS-001`/`ERR-VAL-001`
# already carry several named `reason`s apiece rather than minting one code
# per distinct mistake a caller can make.


async def split_contour(
    db: AsyncSession,
    parent_id: uuid.UUID,
    *,
    actor: User,
    piece_a: Mapping[str, Any],
    piece_b: Mapping[str, Any],
    source: str,
    accuracy_m: Decimal | None = None,
    survey_date: date | None = None,
    effective_from: date | None = None,
) -> tuple[Contour, ContourVersion, Contour, ContourVersion]:
    """Create both subcontours and their first (draft) versions, or neither.

    **Geometry is computed CLIENT-SIDE, validated SERVER-SIDE.** The adminka's
    `splitContour.ts` already cuts a polygon with a line (buffer the line into
    a thin blade, subtract it with `@turf/difference`), tested six ways —
    re-deriving that in PostGIS would duplicate an already-tested algorithm
    for no gain, and PostGIS ships no dedicated "split by line" primitive of
    its own to begin with (SFCGAL's `ST_Split` is not part of this project's
    stack). What atomicity actually needs is not a second cutting engine —
    it is ONE place that persists both pieces together or not at all, and a
    check that neither piece is wrong in a way the client's own turf logic
    could not have caught (a stale drawing, a bug, or a client that skips
    validation entirely). `repo.split_partition_metrics` is that check: it
    normalises both submitted pieces through the SAME pipeline
    `insert_version` stores through, then asks PostGIS whether they
    genuinely partition the parent's own published geometry — no gap, no
    overlap beyond this module's own tolerance setting
    (`gis_overlap_tolerance_m2`, the same one `checks._overlap` already
    reads: ruling 15's "a shared border is a touch of zero area" applies to
    two subcontours' shared edge exactly as it applies to two unrelated
    contours').

    Refusals, checked in this order (identity/state first, the one
    PostGIS round trip last):

    - `ERR-GIS-005 already_split` — `parent_id` already has at least one
      child. A second split would leave the hierarchy ambiguous about which
      pair of subcontours is the authoritative one.
    - `ERR-GIS-005 parent_not_published` — nothing to partition yet: a draft
      or never-drawn contour has no geometry of record for the two pieces to
      reconstruct.
    - `ERR-GIS-005 parent_has_no_geometry` — decision #178: the parent's
      published version was filed by requisites alone. There is no shape for
      the two pieces to reconstruct either way, but this is a DIFFERENT state
      from `parent_not_published` (the version genuinely is published) and a
      caller acting on the refusal needs to tell them apart — attaching real
      geometry to this contour later is a new version, `POST .../versions`,
      never a split. Checked before the one PostGIS round trip
      (`repo.split_partition_metrics`) reaches a NULL parent geometry, which
      that function's own SQL has no branch for (every `CASE` there guards
      the PIECES' own nullability, never the parent's) — unguarded, it would
      leave `mismatch_m2` `None` for an entirely different reason than
      `piece_zero_area` names, and the `assert` right after reading it back
      would raise `AssertionError`, a 500, instead of a clean refusal.
    - `ERR-GIS-005 active_permit_on_parent` — the occupancy seam
      (`occupancy_ha`, filled by `permits.service.occupancy_provider` per
      `app/event_subscriptions.py`; ZERO through this seam with nothing
      registered, never read as "safe to split") reports non-zero occupancy:
      something live still depends on the parent meaning the whole area, so
      the split is refused rather than leaving that permit's occupancy
      pointing at a footprint now nominally divided in two. This is the one
      check on this list that reaches `permits` at all, and it does so
      ONLY through this pre-existing provider registry, never by importing
      `permits.repo`/`.models` (module layering, `docs/design/01`).
    - `ERR-GIS-006 piece_zero_area` — a submitted piece normalises to nothing
      (`repo.split_partition_metrics` reports `None` for its area).
    - `ERR-GIS-006 pieces_overlap` — the two pieces' mutual intersection
      exceeds tolerance: not a shared border, a real overlap.
    - `ERR-GIS-006 pieces_do_not_cover_parent` — the two pieces' union misses
      part of the parent, or extends outside it, beyond tolerance (the SAME
      number catches both directions of mismatch — see
      `repo.split_partition_metrics`'s own docstring).

    Malformed GeoJSON in either piece surfaces as `ERR-GIS-001
    unreadable_geometry` — the exact code/reason `create_version` already
    uses for the same PostGIS parse failure (`ST_GeomFromGeoJSON`'s SQLSTATE
    class XX raise), never a split-specific code for what is the same defect
    everywhere else in this module.

    Once every check passes, both subcontours and their first versions are
    created through `create_contour`/`create_version` THEMSELVES — never a
    parallel insert path — so a split produces identity rows and audit
    entries (`contour.create` × 2, `contour_version.create` × 2)
    indistinguishable from two hand-drawn subcontours, and any future change
    to either function (a new validation, a new audit field) covers a split
    automatically. Both new versions start `draft`, like any other new
    version — the Draft → Review → Approved → Published lifecycle is not
    bypassed; a specialist still submits each for review and a rahbar still
    approves and publishes each, independently (one piece can be published
    before the other, and either can be sent back for rework without
    disturbing its sibling). One extra audit row, `contour.split`, ties the
    two pairs together under the parent's own id — without it, confirming
    "these two contours came from the same split" would mean correlating
    four separate rows by timestamp alone.

    A failure anywhere in this sequence — a duplicate `number`
    (`ERR-GIS-005 number_taken`, from `create_contour`'s own existing
    handling) or anything else — raises immediately and creates nothing:
    `get_db` rolls back the WHOLE transaction on any exception (`app/core/
    deps.py`), so a duplicate number on the SECOND piece undoes the first
    piece's already-flushed insert too. This is what makes the endpoint
    atomic without a manual two-phase undo: nothing here ever needs to know
    how to reverse a partial split, because a partial split can never reach
    the response.
    """
    parent = await repo.contour_by_id(db, parent_id)
    if parent is None:
        raise err("ERR-SYS-003")
    await _assert_in_zone(db, actor, parent.organization_id)
    if await repo.has_children(db, parent_id):
        raise err("ERR-GIS-005", details={"reason": "already_split"})
    parent_version = await repo.published_version(db, parent_id)
    if parent_version is None:
        raise err("ERR-GIS-005", details={"reason": "parent_not_published"})
    if parent_version.geom is None:
        raise err("ERR-GIS-005", details={"reason": "parent_has_no_geometry"})
    occupied_ha, _source = await occupancy_ha(db, parent_id)
    if occupied_ha > Decimal("0"):
        raise err("ERR-GIS-005", details={"reason": "active_permit_on_parent"})

    tolerance = await settings_store.get_int(db, "gis_overlap_tolerance_m2")
    try:
        metrics = await repo.split_partition_metrics(
            db,
            parent_version_id=parent_version.id,
            piece_a_geojson=piece_a["geom"],
            piece_b_geojson=piece_b["geom"],
        )
    except DBAPIError as exc:
        # Same reasoning as `create_version`'s own clause: a genuine PostGIS
        # parse failure (malformed GeoJSON) poisons the session, so raise
        # immediately and touch `db` no further on this path.
        raise err("ERR-GIS-001", details={"reason": "unreadable_geometry"}) from exc
    if metrics["area_a_m2"] is None:
        raise err("ERR-GIS-006", details={"reason": "piece_zero_area", "piece": "a"})
    if metrics["area_b_m2"] is None:
        raise err("ERR-GIS-006", details={"reason": "piece_zero_area", "piece": "b"})
    # Both areas above are real (neither `None`), so `repo.split_partition_metrics`'
    # own CASE logic guarantees these two are too — never independently `None`
    # while both pieces are non-empty. The assert is for pyright's narrowing,
    # not a real runtime possibility.
    intersection_m2, mismatch_m2 = metrics["intersection_m2"], metrics["mismatch_m2"]
    assert intersection_m2 is not None and mismatch_m2 is not None
    if intersection_m2 > tolerance:
        raise err(
            "ERR-GIS-006",
            details={"reason": "pieces_overlap", "area_m2": float(intersection_m2)},
        )
    if mismatch_m2 > tolerance:
        raise err(
            "ERR-GIS-006",
            details={"reason": "pieces_do_not_cover_parent", "area_m2": float(mismatch_m2)},
        )

    child_a = await create_contour(
        db,
        layer_id=parent.layer_id,
        organization_id=parent.organization_id,
        number=piece_a["number"],
        kind="subcontour",
        parent_id=parent.id,
        actor=actor,
    )
    child_b = await create_contour(
        db,
        layer_id=parent.layer_id,
        organization_id=parent.organization_id,
        number=piece_b["number"],
        kind="subcontour",
        parent_id=parent.id,
        actor=actor,
    )
    version_a = await create_version(
        db,
        child_a.id,
        actor=actor,
        geojson=piece_a["geom"],
        source=source,
        declared_area_ha=piece_a.get("declared_area_ha"),
        accuracy_m=accuracy_m,
        survey_date=survey_date,
        effective_from=effective_from,
    )
    version_b = await create_version(
        db,
        child_b.id,
        actor=actor,
        geojson=piece_b["geom"],
        source=source,
        declared_area_ha=piece_b.get("declared_area_ha"),
        accuracy_m=accuracy_m,
        survey_date=survey_date,
        effective_from=effective_from,
    )
    await audit.log(
        db,
        action="contour.split",
        user_id=actor.id,
        object_type="contour",
        object_id=parent.id,
        new_value={
            "piece_a_contour_id": str(child_a.id),
            "piece_a_version_id": str(version_a.id),
            "piece_b_contour_id": str(child_b.id),
            "piece_b_version_id": str(version_b.id),
        },
    )
    return child_a, version_a, child_b, version_b


async def update_version(
    db: AsyncSession, contour_id: uuid.UUID, version_id: uuid.UUID, *, actor: User, **fields: Any
) -> ContourVersion:
    """`PATCH /gis/contours/{id}/versions/{vid}` — draft only; anything else is a
    409 (a published/archived/etc. version is a fact of record, not editable).

    Decision #178: a geometry-less draft's `area_ha` IS its `declared_area_ha`
    (`repo.insert_version`'s own copy, on write) — patching the latter without
    keeping the former in step would leave `area_ha` reporting a figure the
    caller just corrected away from, silently, for every downstream reader that
    trusts `area_ha` alone (module docstring: "every consumer keeps reading one
    column"). Clearing `declared_area_ha` to `null` on such a version is
    refused outright: it is the ONLY area this version has, and the DB CHECK
    (`geom_or_declared_area`) would otherwise turn this into an `IntegrityError`
    this function has no clause mapping to a clear reason."""
    contour = await repo.contour_by_id(db, contour_id)
    if contour is None:
        raise err("ERR-SYS-003")
    await _assert_in_zone(db, actor, contour.organization_id)
    version = await db.get(ContourVersion, version_id)
    if version is None or version.contour_id != contour_id:
        raise err("ERR-SYS-003")
    if version.status != "draft":
        raise err("ERR-GIS-005", details={"reason": "not_draft"})
    if version.geom is None and "declared_area_ha" in fields:
        declared = fields["declared_area_ha"]
        if declared is None or declared <= 0:
            raise err("ERR-VAL-001", details={"reason": "declared_area_required_without_geometry"})
        fields["area_ha"] = declared
    before = {key: _json_safe(getattr(version, key)) for key in fields}
    for key, value in fields.items():
        setattr(version, key, value)
    await db.flush()
    # An in-place UPDATE leaves onupdate columns expired, not refreshed (lesson).
    await db.refresh(version)
    await audit.log(
        db,
        action="contour_version.update",
        user_id=actor.id,
        object_type="contour_version",
        object_id=version.id,
        old_value=before,
        new_value={key: _json_safe(value) for key, value in fields.items()},
    )
    return version


async def run_version_checks(
    db: AsyncSession, contour_id: uuid.UUID, version_id: uuid.UUID, *, actor: User
) -> list[checks.CheckResult]:
    """`POST /gis/contours/{id}/versions/{vid}/checks` — the on-demand run of the
    four topology checks (`gis.checks`) against one version. Zone-scoped the
    same way every other action on a contour's own version is
    (`_assert_in_zone`); read-only, so no audit row (the audit invariant covers
    state-changing actions, and this changes nothing).
    """
    contour = await repo.contour_by_id(db, contour_id)
    if contour is None:
        raise err("ERR-SYS-003")
    await _assert_in_zone(db, actor, contour.organization_id)
    version = await repo.version_by_id(db, version_id)
    if version is None or version.contour_id != contour_id:
        raise err("ERR-SYS-003")
    return await checks.run_checks(db, version_id=version_id)


# --- Task 5: Draft -> Review -> Approved -> Published -> Archived ------------
#
# `TRANSITIONS` lists every edge of tz/07's lifecycle. The two rework edges
# (`approved` -> `review`, `review` -> `draft`) sat in this table with no route
# driving either until the final review of 3.6a: a version `publish_import`
# blocked could then never be edited, sent back or archived — `update_version`
# refuses anything but `draft` and `archive_version` requires `published` — so
# it was stuck at `approved` forever, while `publish_import`'s own docstring
# and design/03 both promise the operator can fix it and re-run. Every edge is
# now reachable (`return_to_review`/`return_to_draft` below).

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("review",),
    "review": ("approved", "draft"),  # sending back to draft is a rework
    "approved": ("published", "review"),
    "published": ("archived",),
    "archived": (),
}


def _assert_transition(version: ContourVersion, target: str) -> None:
    """A transition not listed in `TRANSITIONS[version.status]` is a conflict
    with the version's CURRENT state, not a malformed request body — so this
    raises `ERR-GIS-005` (409), the same code `create_version`/`update_version`
    already use for `version_conflict`/`not_draft`/`number_taken`, never
    `ERR-VAL-001` (422, reserved for the request body itself being wrong)."""
    if target not in TRANSITIONS[version.status]:
        raise err(
            "ERR-GIS-005",
            details={"reason": "bad_transition", "from": version.status, "to": target},
        )


def _assert_transition_from(version: ContourVersion, source: str, target: str) -> None:
    """`_assert_transition` alone is ambiguous wherever two source states share
    one target: `TRANSITIONS` allows `review` from BOTH `draft` (submit-review)
    and `approved` (the rework edge), so a bare "may this become `review`?"
    would let `return-to-review` — `CONTOURS_APPROVE` — also drive
    `draft` -> `review`, which is `submit_review`'s edge and `CONTOURS_MANAGE`.
    An approver could then advance a specialist's draft they may not otherwise
    touch. The mirror is just as real and older: `submit_review` on an ALREADY
    APPROVED version drove `approved` -> `review` under `CONTOURS_MANAGE`.
    Every route touching `review` therefore names the state it is the way OUT
    of, not only the state it leads to (the first direction was caught by this
    fix wave's own bad-transition test, which returned 200 before this existed;
    the second by the scoped re-review that followed)."""
    if version.status != source:
        raise err(
            "ERR-GIS-005",
            details={"reason": "bad_transition", "from": version.status, "to": target},
        )
    _assert_transition(version, target)


async def _version_and_contour(
    db: AsyncSession, version_id: uuid.UUID
) -> tuple[ContourVersion, Contour]:
    """Shared 404 lookup for the four lifecycle actions below. Deliberately
    does NOT also apply `_assert_in_zone` itself — each of the four callers
    does that explicitly, right after calling this, so a reviewer can confirm
    every single one of the four actually applies it (lesson: 'Zone scoping is
    not a permission check — a read path needs both'; a previous task shipped
    with exactly one write path missing it)."""
    version = await repo.version_by_id(db, version_id)
    if version is None:
        raise err("ERR-SYS-003")
    contour = await repo.contour_by_id(db, version.contour_id)
    if contour is None:
        raise err("ERR-SYS-003")
    return version, contour


async def _assert_approval_doc_active(db: AsyncSession, file_id: uuid.UUID) -> None:
    """Confirms a `media_files` row with this id exists and is not archived —
    an EXISTENCE check, mirroring `auth.service._check_poa_file` (lesson: 'An
    existence check is not a validity check'). It proves the document is ON
    RECORD, nothing about whether it actually authorises THIS contour: that
    judgement is the rahbar's own, made before they ever call this endpoint,
    not something the code can verify. Unlike `_check_poa_file`, this does not
    also check `uploaded_by` or `content_type`: an approval decree can be
    scanned by staff other than the approver and need not be a PDF."""
    file = await db.get(MediaFile, file_id)
    if file is None or file.status != "active":
        raise err("ERR-VAL-001", details={"reason": "approval_doc_not_found"})


async def submit_review(db: AsyncSession, version_id: uuid.UUID, *, actor: User) -> ContourVersion:
    """`POST .../submit-review` — draft -> review, `CONTOURS_MANAGE` (the GIS
    specialist hands their own draft to the rahbar). A plain transition: the
    check suite (Task 4) runs at publish, not here.

    `_assert_transition_from`, not the bare `_assert_transition`: `review` is
    the one state in `TRANSITIONS` with two sources, and `"review" in
    TRANSITIONS["approved"]` is True — so a target-only check let a
    `CONTOURS_MANAGE` holder call this on an APPROVED version and drive
    `approved` -> `review`, which is `return_to_review`'s edge and
    `CONTOURS_APPROVE`. That is the exact mirror of the leak the rework routes
    were guarded against, on the older of the two routes, and it also audited
    the rework under the wrong action code (`contour_version.submit_review`).
    """
    version, contour = await _version_and_contour(db, version_id)
    await _assert_in_zone(db, actor, contour.organization_id)
    _assert_transition_from(version, "draft", "review")
    version.status = "review"
    await db.flush()
    # An in-place UPDATE leaves onupdate columns expired, not refreshed (lesson).
    await db.refresh(version)
    await audit.log(
        db,
        action="contour_version.submit_review",
        user_id=actor.id,
        object_type="contour_version",
        object_id=version.id,
        old_value={"status": "draft"},
        new_value={"status": "review"},
    )
    return version


async def approve_version(
    db: AsyncSession,
    version_id: uuid.UUID,
    *,
    actor: User,
    approval_doc_id: uuid.UUID | None,
) -> ContourVersion:
    """`POST .../approve` — review -> approved, `CONTOURS_APPROVE` (the
    rahbar — role code `leadership` — or `chief_forester`; never the GIS
    specialist who drew the version, so approval is always a second pair of
    eyes). `approval_doc_id` is mandatory here even though the column stays
    nullable through draft/review: the CHECK `published_needs_doc` only fires
    at publish, but the basis document has to be on record before the
    rahbar's own approval means anything, so this endpoint asks for it one
    step earlier and stamps it onto the version together with `approved_by`.
    Zone-checked before the request body is even inspected — same ordering
    `create_contour` already documents ("organization_id is zone-checked
    before anything else"), applied here too.
    """
    version, contour = await _version_and_contour(db, version_id)
    await _assert_in_zone(db, actor, contour.organization_id)
    if approval_doc_id is None:
        raise err("ERR-VAL-001", details={"reason": "approval_doc_id_required"})
    _assert_transition(version, "approved")
    await _assert_approval_doc_active(db, approval_doc_id)
    version.status = "approved"
    version.approval_doc_id = approval_doc_id
    version.approved_by = actor.id
    await db.flush()
    await db.refresh(version)
    await audit.log(
        db,
        action="contour_version.approve",
        user_id=actor.id,
        object_type="contour_version",
        object_id=version.id,
        old_value={"status": "review"},
        new_value={"status": "approved", "approval_doc_id": str(approval_doc_id)},
    )
    return version


async def publish_version(
    db: AsyncSession, version_id: uuid.UUID, *, actor: User
) -> ContourVersion:
    """`POST .../publish` — approved -> published, `CONTOURS_APPROVE`. Runs
    the full check suite (`checks.run_checks`) and refuses with `ERR-GIS-003`
    (422) plus the full report in `details.checks` when a BLOCKING check
    fails — `validity`/`within_fund`/`overlap`; `checks.is_blocked` decides,
    not reimplemented here. A `restrictions` intersection is only a warning
    and never blocks (ruling 16) — that is exactly what `is_blocked` already
    encodes.

    Archives whatever version was published before this one, IN THE SAME
    transaction, flushing between the archive UPDATE and the new published
    status: `uq_contour_published_version` is a partial unique index (`WHERE
    status = 'published'`) and is checked against pending statements too, so
    without the flush the old row has not yet been "seen" as archived when
    the new row's uniqueness is checked, and the update can raise on a
    conflict the flush would already have resolved (lesson).
    """
    version, contour = await _version_and_contour(db, version_id)
    await _assert_in_zone(db, actor, contour.organization_id)
    _assert_transition(version, "published")
    results = await checks.run_checks(db, version_id=version_id)
    if checks.is_blocked(results):
        # See `checks.jsonable`'s own docstring: without this conversion, a
        # real overlap's Decimal/UUID payload raises TypeError while Starlette
        # renders THIS very response, turning the 422 into a 500.
        raise err("ERR-GIS-003", details={"checks": checks.jsonable(results)})
    previous = await repo.published_version(db, version.contour_id)
    if previous is not None:
        previous.status = "archived"
        # Flush the archive before the new published state is written — the
        # partial unique index needs to see it first (lesson).
        await db.flush()
    version.status = "published"
    version.published_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(version)
    await audit.log(
        db,
        action="contour_version.publish",
        user_id=actor.id,
        object_type="contour_version",
        object_id=version.id,
        old_value={"status": "approved"},
        new_value={
            "status": "published",
            "replaced": str(previous.id) if previous is not None else None,
        },
    )
    return version


async def return_to_review(
    db: AsyncSession, version_id: uuid.UUID, *, actor: User
) -> ContourVersion:
    """`POST .../return-to-review` — approved -> review, `CONTOURS_APPROVE`
    (the same actor who approved it takes the approval back). This is the
    first half of the way OUT of a stuck state: `publish_import` leaves a
    version a check blocked at `approved`, and nothing else in this module
    moves it — `update_version` refuses anything but `draft`,
    `archive_version` requires `published`.

    `approval_doc_id`/`approved_by` are deliberately LEFT on the row: the
    document was really produced and really signed, and clearing it would
    erase that fact from the record. `approve_version` overwrites both when
    the version is approved again, which is where a correction belongs.
    """
    version, contour = await _version_and_contour(db, version_id)
    await _assert_in_zone(db, actor, contour.organization_id)
    _assert_transition_from(version, "approved", "review")
    version.status = "review"
    await db.flush()
    # An in-place UPDATE leaves onupdate columns expired, not refreshed (lesson).
    await db.refresh(version)
    await audit.log(
        db,
        action="contour_version.return_to_review",
        user_id=actor.id,
        object_type="contour_version",
        object_id=version.id,
        old_value={"status": "approved"},
        new_value={"status": "review"},
    )
    return version


async def return_to_draft(
    db: AsyncSession, version_id: uuid.UUID, *, actor: User
) -> ContourVersion:
    """`POST .../return-to-draft` — review -> draft, `CONTOURS_MANAGE` (the GIS
    specialist takes their own submission back to the bench). The second half
    of the way out: only a `draft` is editable, so this is what makes a
    blocked version fixable at all, and `submit_review` then sends it round
    the same cycle again."""
    version, contour = await _version_and_contour(db, version_id)
    await _assert_in_zone(db, actor, contour.organization_id)
    _assert_transition_from(version, "review", "draft")
    version.status = "draft"
    await db.flush()
    # An in-place UPDATE leaves onupdate columns expired, not refreshed (lesson).
    await db.refresh(version)
    await audit.log(
        db,
        action="contour_version.return_to_draft",
        user_id=actor.id,
        object_type="contour_version",
        object_id=version.id,
        old_value={"status": "review"},
        new_value={"status": "draft"},
    )
    return version


async def archive_version(
    db: AsyncSession, version_id: uuid.UUID, *, actor: User
) -> ContourVersion:
    """`POST .../archive` — published -> archived, `CONTOURS_APPROVE`. For
    taking a published version out of force WITHOUT replacing it (e.g. the
    leshoz stopped issuing permits over that contour). `publish_version`'s own
    supersede step archives the version it replaces as a side effect — never
    through this endpoint, no separate request is made for that case."""
    version, contour = await _version_and_contour(db, version_id)
    await _assert_in_zone(db, actor, contour.organization_id)
    _assert_transition(version, "archived")
    version.status = "archived"
    await db.flush()
    await db.refresh(version)
    await audit.log(
        db,
        action="contour_version.archive",
        user_id=actor.id,
        object_type="contour_version",
        object_id=version.id,
        old_value={"status": "published"},
        new_value={"status": "archived"},
    )
    return version


# --- Task 6: layer_features — restriction, protection and fire-ban layers ---
#
# These layers have NO review step. `tz/07` gives the Draft->Review->
# Approved->Published->Archived lifecycle to CONTOURS only — a layer feature
# goes draft -> published -> archived, full stop. Do not "restore" a missing
# review stage here later; there was never one to begin with.

FEATURE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("published",),
    "published": ("archived",),
    "archived": (),
}


def _assert_feature_transition(feature: LayerFeature, target: str) -> None:
    """Same reasoning as `_assert_transition` above: a transition the row's
    CURRENT status does not allow is a conflict with its state, not a
    malformed request body — `ERR-GIS-005` (409), never `ERR-VAL-001`."""
    if target not in FEATURE_TRANSITIONS[feature.status]:
        raise err(
            "ERR-GIS-005",
            details={"reason": "bad_transition", "from": feature.status, "to": target},
        )


async def _assert_feature_zone(
    db: AsyncSession, actor: User, organization_id: uuid.UUID | None
) -> None:
    """Extends `_assert_in_zone` for `layer_features`, whose `organization_id`
    is nullable — `contours.organization_id` never is, so `_assert_in_zone`
    itself has no branch for a missing one. `None` here means a
    REPUBLIC-WIDE object (a nationwide fire ban belongs to no single
    organization); ruling (task-6 controller, decision 3, sharpened by
    review): only an actor whose `Zone` is EMPTY ON EVERY AXIS — no region,
    no district, no organization (`Zone` has three independent axes,
    `app/core/abac.py`) — may create, edit, publish or archive one.
    Checking `organization_id` alone let a region- or district-scoped actor
    with no organization of their own pass as "republic-level" and reach
    every leshoz in their region (or the whole country) through a nominally
    republic-wide feature — the same escalation this rule exists to
    prevent, one administrative tier up. When `organization_id` IS set,
    this is exactly `_assert_in_zone`'s own all-three-axes rule, applied to
    every write below the same way every contour write already applies it."""
    if organization_id is None:
        if zone_of(actor) != Zone(None, None, None):
            raise err("ERR-ACL-001")
        return
    await _assert_in_zone(db, actor, organization_id)


async def create_feature(
    db: AsyncSession,
    code: str,
    *,
    geojson: dict[str, Any],
    actor: User,
    organization_id: uuid.UUID | None = None,
    name: dict[str, Any] | None = None,
    props: dict[str, Any] | None = None,
    valid_from: date | None = None,
    valid_to: date | None = None,
) -> LayerFeature:
    """`POST /gis/layers/{code}/features` — a restriction, protection zone,
    fire ban or any other non-contour layer object (tz/07 items 8/9/14).

    `geom`'s TYPE is checked against the layer's own declared `geometry_type`
    (`repo.GEOMETRY_TYPE_FAMILIES`) rather than silently reduced to it:
    `repo.insert_version`'s `ST_CollectionExtract(..., 3)` would throw away
    the points `water_points` and lines `cattle_corridors` need (task-6
    brief's design note) — a mismatch is `ERR-VAL-001`,
    reason=geometry_type_mismatch, never a 500 or a silently-wrong shape.
    """
    layer = await repo.layer_by_code(db, code)
    if layer is None:
        raise err("ERR-SYS-003")
    await _assert_feature_zone(db, actor, organization_id)
    try:
        geometry_type = await repo.feature_geometry_type(db, geojson)
    except DBAPIError as exc:
        # Malformed GeoJSON: ST_GeomFromGeoJSON's own parse failure poisons
        # the session the same way create_version's DBAPIError branch
        # documents — raise immediately, no further `db` use on this path.
        raise err("ERR-GIS-001", details={"reason": "unreadable_geometry"}) from exc
    if geometry_type is None:
        raise err("ERR-GIS-001", details={"reason": "empty_geometry"})
    allowed = repo.GEOMETRY_TYPE_FAMILIES.get(layer.geometry_type, ())
    if allowed and geometry_type not in allowed:
        raise err("ERR-VAL-001", details={"reason": "geometry_type_mismatch"})
    try:
        feature = await repo.insert_feature(
            db,
            layer_id=layer.id,
            geojson=geojson,
            organization_id=organization_id,
            name=name,
            props=props if props is not None else {},
            valid_from=valid_from,
            valid_to=valid_to,
            created_by=actor.id,
        )
    except IntegrityError as exc:
        # The one FK in this INSERT a caller actually supplies a value for —
        # layer_id/created_by always come from a real layer/actor row, never
        # the request body. Session is poisoned after this, same reasoning as
        # create_contour's own IntegrityError handling — raise immediately.
        raise err("ERR-VAL-001", details={"reason": "organization_not_found"}) from exc
    await audit.log(
        db,
        action="layer_feature.create",
        user_id=actor.id,
        object_type="layer_feature",
        object_id=feature.id,
        new_value={
            "layer_id": str(layer.id),
            "layer_code": code,
            "organization_id": str(organization_id) if organization_id is not None else None,
            "valid_from": _json_safe(valid_from),
            "valid_to": _json_safe(valid_to),
        },
    )
    return feature


async def update_feature(
    db: AsyncSession, feature_id: uuid.UUID, *, actor: User, **fields: Any
) -> LayerFeature:
    """`PATCH /gis/layers/{code}/features/{id}` — draft-only metadata edits
    (name/props/valid_from/valid_to); geometry is never patched in place, the
    same 'a changed shape is a new object' rule `update_version` applies to
    contours."""
    feature = await repo.feature_by_id(db, feature_id)
    if feature is None:
        raise err("ERR-SYS-003")
    await _assert_feature_zone(db, actor, feature.organization_id)
    if feature.status != "draft":
        raise err("ERR-GIS-005", details={"reason": "not_draft"})
    before = {key: _json_safe(getattr(feature, key)) for key in fields}
    for key, value in fields.items():
        setattr(feature, key, value)
    try:
        await db.flush()
    except IntegrityError as exc:
        # The one CHECK a partial PATCH can still violate that
        # FeaturePatch's own model_validator cannot see: patching only ONE
        # side of an existing valid_from/valid_to pair (task-6 controller,
        # decision 4 — exactly why the DB CHECK stays even though pydantic
        # already covers the same-request case). Session poisoned after
        # this, same reasoning as create_version's own handling — raise
        # immediately, no further `db` use on this path.
        raise err("ERR-VAL-001", details={"reason": "validity_period_invalid"}) from exc
    await db.refresh(feature)
    await audit.log(
        db,
        action="layer_feature.update",
        user_id=actor.id,
        object_type="layer_feature",
        object_id=feature.id,
        old_value=before,
        new_value={key: _json_safe(value) for key, value in fields.items()},
    )
    return feature


async def publish_feature(db: AsyncSession, feature_id: uuid.UUID, *, actor: User) -> LayerFeature:
    """`POST .../features/{id}/publish` — draft -> published. No check suite
    here (unlike `publish_version`'s topology gate): these layers carry no
    geometry rule of their own to satisfy — they ARE what the contour checks
    read (`checks._restrictions`) — and there is no review step to have
    passed first (decision 5: draft -> published -> archived, full stop)."""
    feature = await repo.feature_by_id(db, feature_id)
    if feature is None:
        raise err("ERR-SYS-003")
    await _assert_feature_zone(db, actor, feature.organization_id)
    _assert_feature_transition(feature, "published")
    feature.status = "published"
    await db.flush()
    await db.refresh(feature)
    await audit.log(
        db,
        action="layer_feature.publish",
        user_id=actor.id,
        object_type="layer_feature",
        object_id=feature.id,
        old_value={"status": "draft"},
        new_value={"status": "published"},
    )
    return feature


async def archive_feature(db: AsyncSession, feature_id: uuid.UUID, *, actor: User) -> LayerFeature:
    """`POST .../features/{id}/archive` — published -> archived, the end of
    the line (decision 5). Unlike `publish_version`, there is no 'replace and
    archive the previous one' step here — a feature never supersedes another
    one automatically; each is archived on its own, explicit call."""
    feature = await repo.feature_by_id(db, feature_id)
    if feature is None:
        raise err("ERR-SYS-003")
    await _assert_feature_zone(db, actor, feature.organization_id)
    _assert_feature_transition(feature, "archived")
    feature.status = "archived"
    await db.flush()
    await db.refresh(feature)
    await audit.log(
        db,
        action="layer_feature.archive",
        user_id=actor.id,
        object_type="layer_feature",
        object_id=feature.id,
        old_value={"status": "published"},
        new_value={"status": "archived"},
    )
    return feature


# --- Task 8: batch publication + the read API for 3.7/3.9 --------------------
#
# The Agency delivers whole leshozes, not one contour at a time, so ruling 3
# gives the WHOLE BATCH one basis document, one review and one publication —
# but every version it created still goes through the SAME submit_review /
# approve_version / publish_version above as a hand-drawn one, called in a
# loop. There is exactly one implementation of each lifecycle step; these
# three functions only decide WHICH versions to call it on and how to read the
# batch's own `gis_imports.status` alongside them.


async def _load_import_for_transition(
    db: AsyncSession, import_id: uuid.UUID, *, actor: Any, expected_status: str
) -> GisImport:
    """Shared preamble for all three batch actions below: row lookup, zone
    check, layer check, status check — factored out after the re-review found
    the layer check ALONE in `submit_import_review` closed only one of three
    doors. `approve_import`/`publish_import` gate on `row.status` alone and
    require `CONTOURS_APPROVE`, a DIFFERENT permission from
    `submit_import_review`'s `CONTOURS_MANAGE` — so an actor holding only the
    former (the rahbar) can reach either directly, never having called, or
    been able to call, submit-review at all. Every one of the three now goes
    through this SAME function, so no entry point can be reached without the
    layer check.

    The layer check runs BEFORE the status check, on purpose: a non-contour
    batch called at the "wrong" endpoint for its actual status (e.g.
    `/publish` on a batch still at `review`) reports the useful reason
    (`not_a_contour_batch`) instead of a `bad_transition` that does not say
    why. A `restrictions`/`fire_bans`/etc. batch creates `layer_features`
    rows, not `ContourVersion` ones — without this check, ANY of the three
    would loop zero rows and sail through to a false success. Refused loudly
    here instead of inventing a second, feature-batch lifecycle: those rows
    still publish one at a time through Task 6's own
    `POST /layers/{code}/features/{id}/publish` — no bulk path for them yet,
    a known and named gap, not one this silently papers over.
    """
    row = await repo.import_by_id(db, import_id)
    if row is None:
        raise err("ERR-SYS-003")
    await _assert_in_zone(db, actor, row.organization_id)
    layer = await db.get(GisLayer, row.layer_id)
    if layer is None or layer.code != CONTOUR_LAYER_CODE:
        raise err("ERR-GIS-005", details={"reason": "not_a_contour_batch"})
    if row.status != expected_status:
        raise err("ERR-GIS-005", details={"reason": "bad_transition", "from": row.status})
    return row


def _assert_batch_not_empty(versions: list[ContourVersion]) -> None:
    """A transition that would move zero versions is not a successful no-op —
    it is the same defect `_load_import_for_transition`'s layer check closes,
    reached a different way: a CONTOUR batch that skipped an earlier step
    (e.g. `/approve` called before `/submit-review`, so every version is
    still `draft` and the `review`-status query finds nothing) would
    otherwise silently advance `gis_imports.status` without moving a single
    version.

    `approve_import` is the only caller left: `submit_import_review` and
    `publish_import` each need to tell one particular already-finished shape
    apart from a genuinely empty batch, and do it inline."""
    if not versions:
        raise err("ERR-GIS-005", details={"reason": "empty_batch"})


async def submit_import_review(db: AsyncSession, import_id: uuid.UUID, *, actor: Any) -> GisImport:
    """`POST /gis/imports/{id}/submit-review` — `CONTOURS_MANAGE` (the GIS
    specialist who ran the import submits their own batch, same actor who
    would submit one hand-drawn draft). Task 7 already leaves the batch row at
    `status='review'` the moment it parses cleanly — this action does not move
    THAT status at all; its real effect is cascading every version the import
    created from `draft` to `review`, one `submit_review` call each, so
    `approve_import` below has something in the right state to work on."""
    row = await _load_import_for_transition(db, import_id, actor=actor, expected_status="review")
    versions = await repo.import_versions(db, import_id, status="draft")
    if not versions:
        # Still a 409 either way — this action really did nothing, and
        # advancing anything here would jump the batch a state it never
        # earned. Only the REASON is sharpened: a second `/submit-review` on a
        # batch already at `review` or beyond is an operator double-click, not
        # the "this batch has no versions at all" defect `empty_batch` names.
        statuses = await repo.import_version_statuses(db, import_id)
        if statuses:
            raise err("ERR-GIS-005", details={"reason": "already_submitted"})
        raise err("ERR-GIS-005", details={"reason": "empty_batch"})
    for version in versions:
        await submit_review(db, version.id, actor=actor)
    await audit.log(
        db,
        action="gis_import.submit_review",
        user_id=actor.id,
        object_type="gis_import",
        object_id=row.id,
        new_value={"status": row.status},
    )
    return row


async def approve_import(db: AsyncSession, import_id: uuid.UUID, *, actor: Any) -> GisImport:
    """`POST /gis/imports/{id}/approve` — `CONTOURS_APPROVE` (the rahbar).
    Stamps the BATCH's own `approval_doc_id` onto every version it created
    (ruling 3: one basis document for the whole delivery, nobody signs a
    decree per contour) by calling `approve_version` once per version still in
    `review`, then advances the batch row itself to `approved`."""
    row = await _load_import_for_transition(db, import_id, actor=actor, expected_status="review")
    versions = await repo.import_versions(db, import_id, status="review")
    _assert_batch_not_empty(versions)
    for version in versions:
        await approve_version(db, version.id, actor=actor, approval_doc_id=row.approval_doc_id)
    row.status = "approved"
    row.approved_by = actor.id
    await db.flush()
    # An in-place UPDATE leaves onupdate columns expired, not refreshed (lesson).
    await db.refresh(row)
    await audit.log(
        db,
        action="gis_import.approve",
        user_id=actor.id,
        object_type="gis_import",
        object_id=row.id,
        old_value={"status": "review"},
        new_value={"status": "approved"},
    )
    return row


async def publish_import(db: AsyncSession, import_id: uuid.UUID, *, actor: Any) -> dict[str, Any]:
    """`POST /gis/imports/{id}/publish` — `CONTOURS_APPROVE`. Ruling 3: the
    batch is the unit of publication, but each version goes through the SAME
    `publish_version` a hand-made one uses, one implementation called in a
    loop — never a second, batch-only publish path.

    `DomainError` is caught deliberately narrowly: only `ERR-GIS-003` (the
    check report) means "this one version cannot publish" — a blocked version
    keeps its `approved` status, is reported in `blocked[]` with its own check
    report, and does not stop its siblings, so a 151-feature delivery is never
    hostage to one bad polygon. Anything else — a bad transition, a missing
    row, a database error — is a defect of the batch itself, not a per-feature
    business outcome, and must propagate and roll the whole call back;
    catching `Exception` here would turn a broken migration into a silent
    "0 published" instead of a loud failure.

    The batch reaches `done` only when nothing was blocked; otherwise it stays
    `approved` so the operator can fix the offending feature and re-run —
    `publish_import` is safe to call again, since an already-`published`
    version is simply absent from the next `status='approved'` batch. When
    that leaves NO approved versions but every version of the batch is
    `published`, the batch is finished and this reports it as such
    (`done`, `{"published": 0, "blocked": []}`) rather than 409-ing a batch
    that has in fact completed — the operator's own last fix may well have
    gone through the single-version route.
    """
    row = await _load_import_for_transition(db, import_id, actor=actor, expected_status="approved")
    versions = await repo.import_versions(db, import_id, status="approved")
    if not versions:
        # The corollary of `_assert_batch_not_empty`, and ONLY here: a batch
        # whose every version is already `published` has nothing left to do,
        # and the row sitting at `approved` is the only thing still saying
        # otherwise. That happens for real — the operator publishes the last
        # blocked version through the single-version route after fixing it, and
        # `publish_import` is documented as safe to call again. Reporting
        # `empty_batch` there would be a 409 for a batch that is, in fact,
        # done. `submit_import_review`/`approve_import` deliberately do NOT get
        # this: for them it would advance the row two states from an action
        # that moved nothing.
        statuses = await repo.import_version_statuses(db, import_id)
        if statuses and all(status == "published" for status in statuses):
            row.status = "done"
            row.stats = {**(row.stats or {}), "published": 0, "blocked": 0}
            await db.flush()
            await audit.log(
                db,
                action="gis_import.publish",
                user_id=actor.id,
                object_type="gis_import",
                object_id=row.id,
                old_value={"status": "approved"},
                new_value={"status": "done", "published": 0, "blocked": 0},
            )
            return {"published": 0, "blocked": []}
        raise err("ERR-GIS-005", details={"reason": "empty_batch"})
    published, blocked = 0, []
    for version in versions:
        try:
            await publish_version(db, version.id, actor=actor)
            published += 1
        except DomainError as exc:
            if exc.code != "ERR-GIS-003":
                raise
            assert exc.details is not None  # publish_version always sets details={"checks": ...}
            blocked.append({"version_id": str(version.id), "checks": exc.details["checks"]})
    row.status = "done" if not blocked else "approved"
    row.stats = {**(row.stats or {}), "published": published, "blocked": len(blocked)}
    await db.flush()
    await audit.log(
        db,
        action="gis_import.publish",
        user_id=actor.id,
        object_type="gis_import",
        object_id=row.id,
        new_value={"published": published, "blocked": len(blocked)},
    )
    return {"published": published, "blocked": blocked}


# The occupancy seam (ruling 3/14): `gis` must never import `permits` (which
# does not exist yet), so a future stage plugs in here instead — the same
# registration idiom as `core.files.ACCESS_CHECKS`. With nothing registered
# (true today), occupancy is an explicit placeholder, never a silent zero that
# could be mistaken for a real measurement.
#
# The signature is BATCH-shaped — a whole page of contour ids in, a mapping
# out — because `list_contours` needs one answer per row and the per-contour
# shape made that a query per row: 3.11's very first registration would have
# turned a page of 20 into 20 round-trips, and the whole-country list into
# ~13,500. A provider is free to answer with fewer keys than it was asked
# about; a contour it says nothing about counts as zero.
OccupancyProvider = Callable[
    [AsyncSession, Sequence[uuid.UUID]], Awaitable[Mapping[uuid.UUID, Decimal]]
]
OCCUPANCY_PROVIDERS: list[OccupancyProvider] = []


async def occupancy_map(
    db: AsyncSession, contour_ids: Sequence[uuid.UUID]
) -> tuple[dict[uuid.UUID, Decimal], str]:
    """Sum every registered provider's answer for a whole page of contours, in
    one call per provider. `Decimal`, never `float` (project convention: areas
    are numeric) — quantized to 4 dp to match `contour_versions.area_ha`'s own
    NUMERIC(12,4) scale, the figure `s_available_ha` is subtracted against.
    With no provider registered the source is `"none"` and every figure is an
    explicit `Decimal('0.0000')`, so a reader can never mistake this
    placeholder for a measurement; once something registers, the source flips
    to `"permits"` (ruling 14). Keys a provider returns that were not asked
    about are ignored — the caller decides the page, not the provider."""
    if not OCCUPANCY_PROVIDERS:
        return {contour_id: Decimal("0.0000") for contour_id in contour_ids}, "none"
    totals = {contour_id: Decimal("0") for contour_id in contour_ids}
    for provider in OCCUPANCY_PROVIDERS:
        for contour_id, occupied in (await provider(db, list(contour_ids))).items():
            if contour_id in totals:
                totals[contour_id] += occupied
    return {k: v.quantize(Decimal("0.0001")) for k, v in totals.items()}, "permits"


async def occupancy_ha(db: AsyncSession, contour_id: uuid.UUID) -> tuple[Decimal, str]:
    """One contour's occupancy — the single-card path (`contour_card`), over
    the same batch call, so there is one summation rule and not two."""
    totals, source = await occupancy_map(db, [contour_id])
    return totals[contour_id], source


def _available_ha(area_ha: Decimal, occupied_ha: Decimal) -> tuple[Decimal, bool]:
    """`s_available_ha` and whether the contour is over-allocated.

    Two permits issued over the same whole parcel (the Burchmulla demo's
    `10517қ`: -65.0694 ga reported against a 65.0694 ga total) make
    `area_ha - occupied_ha` negative — a true state, not a measurement bug
    (`occupancy_provider`'s own docstring: counting every ACTIVE permit with
    no period filter is the DELIBERATE conservative direction, "never
    over-book" stated as a ceiling on how FREE the figure may read, not a
    promise it can never go negative). A raw negative reads as nonsense to
    any consumer computing "how much can still be requested" — but silently
    flooring it to zero would erase the one signal that the allocation is
    wrong, so both are reported: `s_available_ha` floored at zero (what
    remains that CAN be allocated) alongside an explicit `over_allocated`
    flag (that the parcel already has too much committed against it) —
    "loudly wrong beats silently wrong" (`CLAUDE.md`'s own phrase for
    `coef_sb:*`/`benefit_proof`), never a value an operator has to notice by
    subtracting two other fields themselves.
    """
    raw = area_ha - occupied_ha
    return max(raw, Decimal("0")), raw < Decimal("0")


def _parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    """Four comma-separated finite WGS84 degrees — anything else (wrong count,
    non-numeric, non-finite, out of range, min greater than max) is
    `ERR-VAL-001` (422), never a 500 (ruling 4: a bare `float()` on user input
    inside a route is exactly how a `ValueError` becomes one).

    `float()` also accepts `nan`, `inf` and `-inf`, and every comparison
    against NaN is False — so `nan,nan,nan,nan` sailed through both guards
    into `ST_MakeEnvelope`, where PostGIS raises and `app/main.py`, which has
    no `DBAPIError` handler, turns it into a 500 on two endpoints every
    authenticated user can reach (`GET /gis/contours`, `GET /gis/layers/
    {code}/features`). `math.isfinite` runs BEFORE the range check for the same
    reason: a NaN would silently pass `-180 <= x <= 180` as False either way,
    but stating the rule explicitly is what keeps the next reader from
    re-deriving it.
    """
    if bbox is None:
        return None
    parts = bbox.split(",")
    if len(parts) != 4:
        raise err("ERR-VAL-001", details={"reason": "bbox_invalid"})
    try:
        values = [float(part) for part in parts]
    except ValueError:
        raise err("ERR-VAL-001", details={"reason": "bbox_invalid"}) from None
    if not all(math.isfinite(value) for value in values):
        raise err("ERR-VAL-001", details={"reason": "bbox_invalid"})
    min_lon, min_lat, max_lon, max_lat = values
    if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
        raise err("ERR-VAL-001", details={"reason": "bbox_out_of_range"})
    if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
        raise err("ERR-VAL-001", details={"reason": "bbox_out_of_range"})
    if min_lon > max_lon or min_lat > max_lat:
        raise err("ERR-VAL-001", details={"reason": "bbox_invalid"})
    return min_lon, min_lat, max_lon, max_lat


async def list_contours(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID | None = None,
    bbox: str | None = None,
    params: PageParams,
    actor: User,
) -> tuple[list[dict[str, Any]], int]:
    """`GET /gis/contours` — picking a plot (design/03, C3 item 3). PAGED
    (`Page[T]`/`PageParams`, `?page=1&page_size=20`, max 100 — design/03's own
    convention, the same envelope `/admin/users` uses): unbounded, this
    answered every published contour in the country, ~13,500 rows once the
    leshozes land. Joins to
    each contour's PUBLISHED version only (decision 6): a draft has no
    geometry of record yet, so it is invisible here regardless of the caller's
    role — which is exactly what makes an applicant see published contours
    only, with no role branch of its own. `zone_filter` narrows this to the
    actor's own zone; it is a no-op (`true()`) for a republic-wide staff
    member or an applicant.

    All THREE zone axes are supplied (review finding 2), even though `Contour`
    itself only carries `organization_id`: `zone_filter` fails closed and
    RAISES when a zone axis is set but its column is not.
    `admin.users_service.create_user` sets region_id/district_id/organization_id
    independently with no cross-validation, so a region- or district-scoped,
    organization-less actor is real and reachable here; `repo.list_contours`
    joins `organizations` so `Organization.region_id`/`district_id` are
    available to check against — the same resolution `_assert_in_zone` does
    row-by-row on the write paths."""
    parsed_bbox = _parse_bbox(bbox)
    zone = zone_filter(
        zone_of(actor),
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Contour.organization_id,
    )
    rows, total = await repo.list_contours(
        db,
        organization_id=organization_id,
        bbox=parsed_bbox,
        zone=zone,
        offset=params.offset,
        limit=params.page_size,
    )
    occupied_by_id, source = await occupancy_map(db, [row.contour_id for row in rows])
    items = []
    for row in rows:
        available, over_allocated = _available_ha(row.area_ha, occupied_by_id[row.contour_id])
        items.append(
            {
                "id": row.contour_id,
                "number": row.number,
                "organization_id": row.organization_id,
                "area_ha": row.area_ha,
                "occupied_ha": occupied_by_id[row.contour_id],
                "s_available_ha": available,
                "over_allocated": over_allocated,
                "occupancy_source": source,
            }
        )
    return items, total


async def list_contour_features(
    db: AsyncSession,
    *,
    bbox: str | None = None,
    organization_id: uuid.UUID | None = None,
    actor: User,
) -> dict[str, Any]:
    """`GET /gis/contours/features` — the whole published contour layer as
    GeoJSON, for a map to draw before anything is picked.

    Visibility is `list_contours`' exactly, built the same way from the same
    three zone axes: an applicant and a republic-wide staff member see every
    published contour, a leshoz-scoped one sees their own. Deriving it here a
    second time rather than sharing a helper is the one thing NOT done — the
    filter is built by the same `zone_filter` call with the same columns, so a
    change to who may see a contour cannot land on the list and miss the map.

    No permission code, matching `list_contours` and `contour_card`: which
    parcels exist and where they lie is what an applicant needs before they
    can ask for anything, and all three answer published versions only.
    """
    parsed_bbox = _parse_bbox(bbox)
    zone = zone_filter(
        zone_of(actor),
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Contour.organization_id,
    )
    return await repo.contour_features_geojson(
        db, bbox=parsed_bbox, zone=zone, organization_id=organization_id
    )


async def contour_card(db: AsyncSession, contour_id: uuid.UUID, *, actor: User) -> dict[str, Any]:
    """`GET /gis/contours/{id}` — the published version's geometry plus the
    same occupancy placeholder `list_contours` carries (ruling 14). Requires a
    published version to exist, for every role alike (mirrors `list_contours`'
    own join) — a contour that never reached `published` has nothing here yet
    to show as its card.

    Decision #178: `geometry` is `None` whenever there is none to show —
    either the version itself carries no `geom` (`ST_AsGeoJSON(NULL)` is
    `NULL`, and `json.loads(None)` raises `TypeError`, so this can never be
    unconditional again) or the owning organization's `gis_enabled` switch is
    off, in which case `geometry` stays `None` even on the rare row that
    happens to carry real geometry anyway — the switch is the authoritative
    "does this leshoz show a map" answer, matching `repo.contour_features_
    geojson`'s own reasoning for the multi-contour layer. Every OTHER field
    (`area_ha`, `occupied_ha`, `s_available_ha`, …) is unaffected either way:
    pricing and capacity read those, never `geometry`."""
    row = await repo.contour_card(db, contour_id)
    if row is None:
        raise err("ERR-SYS-003")
    occupied, source = await occupancy_ha(db, contour_id)
    available, over_allocated = _available_ha(row.area_ha, occupied)
    org = await admin_repo.get_organization(db, row.organization_id)
    geometry = json.loads(row.geometry) if row.geometry is not None else None
    if org is None or not org.gis_enabled:
        geometry = None
    return {
        "id": row.contour_id,
        "number": row.number,
        "organization_id": row.organization_id,
        "kind": row.kind,
        "version_id": row.version_id,
        "area_ha": row.area_ha,
        "geometry": geometry,
        "occupied_ha": occupied,
        "s_available_ha": available,
        "over_allocated": over_allocated,
        "occupancy_source": source,
    }


async def list_versions(
    db: AsyncSession,
    contour_id: uuid.UUID,
    *,
    status: str | None,
    params: PageParams,
    actor: User,
) -> tuple[list[ContourVersion], int]:
    """`GET /gis/contours/{id}/versions` — task defect 4a: a contour version
    awaiting approval had no route listing it at all, so the rahbar who must
    approve it, and the specialist tracking their own submission, could only
    be handed a version id out of band. Zone-scoped by the SAME three columns
    `list_contours` checks (`Organization.region_id`/`district_id`,
    `Contour.organization_id`) — a version outside the actor's zone stays
    invisible here exactly as a published contour outside it is invisible on
    the public list. Every status shows unless `status` narrows it; oldest
    first, so the history reads in the order it was actually edited."""
    zone = zone_filter(
        zone_of(actor),
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Contour.organization_id,
    )
    return await repo.list_versions(
        db, contour_id, zone=zone, status=status, offset=params.offset, limit=params.page_size
    )


async def version_detail(
    db: AsyncSession, contour_id: uuid.UUID, version_id: uuid.UUID, *, actor: User
) -> dict[str, Any]:
    """`GET /gis/contours/{id}/versions/{version_id}` — the other half of
    defect 4a: `VersionOut` carries no geometry at all, so even a version id
    handed over by hand could not actually be looked at. Same zone condition
    as `list_versions`; a version outside it answers `ERR-SYS-003`, the same
    not-found shape `contour_card` uses for a contour with no published
    version — existence outside your own zone is not information this route
    hands out.

    `geometry` is `None` for a version filed by requisites alone (decision
    #178) — `ST_AsGeoJSON(NULL)` is `NULL`, and this is the specialist's own
    detail view of THEIR record, so unlike `contour_card` it reports whatever
    is actually there rather than also gating on the organization's
    `gis_enabled` switch."""
    zone = zone_filter(
        zone_of(actor),
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Contour.organization_id,
    )
    row = await repo.version_detail(db, contour_id, version_id, zone=zone)
    if row is None:
        raise err("ERR-SYS-003")
    return {
        "id": row.id,
        "contour_id": row.contour_id,
        "version_no": row.version_no,
        "status": row.status,
        "source": row.source,
        "area_ha": row.area_ha,
        "declared_area_ha": row.declared_area_ha,
        "accuracy_m": row.accuracy_m,
        "survey_date": row.survey_date,
        "effective_from": row.effective_from,
        "approval_doc_id": row.approval_doc_id,
        "approved_by": row.approved_by,
        "published_at": row.published_at,
        "geometry": json.loads(row.geometry) if row.geometry is not None else None,
    }


# --- The public surface for levels 3+ (this module's own docstring) ----------
#
# `published_version` lives in `repo` and `run_checks` in `checks`, and
# `run_version_checks` above needs an HTTP actor plus a `contour_id` that a
# norm-approval flow has no source for. Both are re-exported here as thin
# pass-throughs so 3.7/3.9 never have to import `gis.repo` or `gis.checks`
# directly and break the module-boundary rule (CLAUDE.md: cross-module calls
# go through the other module's `service`).


async def published_version(db: AsyncSession, contour_id: uuid.UUID) -> ContourVersion | None:
    """The one version of this contour currently in force, or `None`. No
    permission or zone rule: the caller is another SERVICE inside this
    process, not an HTTP actor — the gates live on the routes that reach it."""
    return await repo.published_version(db, contour_id)


async def contour_organization(db: AsyncSession, contour_id: uuid.UUID) -> uuid.UUID | None:
    """Which leshoz owns this contour — what a level-3 module needs to apply its
    own zone rule without importing `gis.repo` (CLAUDE.md module boundary)."""
    return await repo.contour_organization(db, contour_id)


async def distance_to_contour_m(
    db: AsyncSession, contour_id: uuid.UUID, *, lon: float, lat: float
) -> Decimal | None:
    """A point's distance to the contour's published version, in metres, or
    `None` when there is no published version to compare against. `inspections`
    (level 5) is today's only caller — an inspector's GPS fix at a field act
    versus the plot being checked (tz/04 С15)."""
    return await repo.distance_to_published_version_m(db, contour_id, lon=lon, lat=lat)


def contour_organization_column(contour_id_col: Any) -> Any:
    """`contour_organization` above, shaped as a SQL expression for a caller
    whose zone rule has to run INSIDE a paged query.

    First consumer: `applications` (level 3, so it reaches this module only
    through this service). An application carries `assigned_org_id`, which is
    null until a reviewer takes it into work, so the organization its zone rule
    must compare against is `coalesce(assigned_org_id, <the contour's owner>)` —
    a per-row resolution the card can do with `contour_organization` above and
    `GET /applications` cannot, because a filter applied after paging is not a
    filter. Returning the correlated subquery keeps every statement over
    `contours` built here, in the module that owns the table.
    """
    return repo.contour_organization_column(contour_id_col)


async def contour_number(db: AsyncSession, contour_id: uuid.UUID) -> str | None:
    """This contour's own number (`tz/13` § 1-илова requisite 9, «ID контура/
    субконтура»), or None if there is no such contour. Identity only, like
    `contour_organization` beside it — no version, no geometry, no occupancy.

    A string rather than the `Contour` row on purpose: 3.11a copies it into an
    immutable permit snapshot and needs nothing else, and a row handed out here
    would be a second, ungated way to read this module's state. `contour_card`
    is not the answer to this question — it requires a published version, needs
    an HTTP actor, and calls back into `OCCUPANCY_PROVIDERS`, which permits
    itself registers."""
    return await repo.contour_number(db, contour_id)


async def version_geometry(db: AsyncSession, version_id: uuid.UUID) -> dict[str, Any] | None:
    """One specific version's geometry, as a parsed GeoJSON geometry object —
    `None` when no such version exists. Like `contour_organization`/
    `contour_number` beside it: no permission, no zone, no HTTP actor at all,
    because the caller is another service inside this process rather than a
    route.

    Keyed on the VERSION, not the contour, on purpose (this replaced an
    earlier `published_contour_geometry(contour_id)` that a review caught
    reading whatever version is published TODAY): `permits.service.
    public_check` must show the geometry the permit was actually issued
    against, `permits.contour_version_id`, which the `ContourVersion` model's
    own docstring says is frozen precisely so "republishing never moves the
    ground under an issued permit" — `gis.service.publish_version` archives
    the old published row with no check for permits still referencing it, so
    a boundary correction or a #91 split after issuance is a normal,
    unguarded operation that must not change what this reads.

    Reuses `repo.version_geometry`'s own `ST_AsGeoJSON` conversion (module
    convention: PostGIS renders geometry, this module never reconstructs it in
    Python) instead of a second query doing the same thing. First and only
    consumer: `permits.service.public_check`, behind
    `public_permit_contour_enabled` (ruling R2, default OFF) — the ONE caller
    with no actor of any kind.
    """
    geometry = await repo.version_geometry(db, version_id)
    return None if geometry is None else json.loads(geometry)


async def run_checks(db: AsyncSession, version_id: uuid.UUID) -> list[checks.CheckResult]:
    """The four topology checks against one version, with no actor and no
    contour id — what a norm or an application pre-check needs.
    `run_version_checks` above is the HTTP-facing sibling: same checks, plus
    the 404 lookup and the zone gate a request has to pass."""
    return await checks.run_checks(db, version_id=version_id)


async def features_intersecting(
    db: AsyncSession,
    contour_id: uuid.UUID,
    layer_codes: Sequence[str],
    period_from: date,
    period_to: date,
) -> list[Any]:
    """Published features of the given layers overlapping this contour's
    published geometry (by more than the tolerance) and valid during the given
    period — `norms.checks`' own fire-ban/restriction split (ruling 15) reads
    this instead of `gis.repo` directly, same reasoning as `run_checks` above."""
    return await repo.features_intersecting(db, contour_id, layer_codes, period_from, period_to)


async def _may_manage_layers(db: AsyncSession, actor: User) -> bool:
    """Holds `gis.layers.manage`, or is the superuser that passes every
    permission gate (decision #41 ruling 2) — the same two-branch shape
    `admin.users_service._may_manage` uses, since this is a rule INSIDE a
    handler rather than a `require_permission` dependency on the route."""
    if await auth_repo.role_code(db, actor) == SUPERUSER_ROLE:
        return True
    return LAYERS_MANAGE in await auth_repo.permission_codes(db, actor)


async def list_features(
    db: AsyncSession,
    code: str,
    *,
    bbox: str | None = None,
    valid_on: date | None = None,
    status: str | None = None,
    import_id: uuid.UUID | None = None,
    actor: User,
) -> dict[str, Any]:
    """`GET /gis/layers/{code}/features` — the GeoJSON a map draws (ruling 5).
    A non-public layer is refused to an actor whose ROLE is `applicant`
    specifically — not a permission gate (every staff role reads any layer's
    features regardless of a held grant, the same way `GET /gis/layers`
    itself is open to any authenticated user, ruling 18).

    `status` defaults to `published`, which is the whole of what the map and
    every applicant ever see. Asking for anything else is an OPERATOR action —
    the only way to learn the ids an import created, since the batch endpoints
    refuse a non-contour batch and `GET /gis/imports/{id}` returns counters
    only — so it is gated behind `gis.layers.manage`, the same permission the
    per-feature publish route uses. Without it a `forest_fund` delivery could
    never be published at all, and `checks._within_fund` would stay `skipped`
    forever. `import_id` narrows to one batch and needs no gate of its own: on
    published rows it reveals nothing a plain listing does not.
    """
    layer = await repo.layer_by_code(db, code)
    if layer is None:
        raise err("ERR-SYS-003")
    if not layer.is_public:
        role = await auth_repo.role_code(db, actor)
        if role == "applicant":
            raise err("ERR-ACL-001")
    if status is not None and status != "published" and not await _may_manage_layers(db, actor):
        raise err("ERR-ACL-001")
    parsed_bbox = _parse_bbox(bbox)
    return await repo.features_geojson(
        db,
        layer_code=code,
        bbox=parsed_bbox,
        valid_on=valid_on,
        status=status or "published",
        import_id=import_id,
    )


async def public_features(
    db: AsyncSession, code: str, *, bbox: str | None = None
) -> dict[str, Any]:
    """The anonymous mirror of `list_features` (4.6 `public`, design/01 rule 2:
    cross-module calls go through the service). No `actor` exists on this path,
    so there is no operator escape hatch and none is needed: always `published`,
    and only a layer marked `is_public` — the identical rule `list_features`
    enforces for an `applicant`, applied here to every caller since anonymous
    IS the least-privileged role."""
    layer = await repo.layer_by_code(db, code)
    if layer is None or not layer.is_public:
        raise err("ERR-SYS-003")
    parsed_bbox = _parse_bbox(bbox)
    return await repo.features_geojson(
        db, layer_code=code, bbox=parsed_bbox, valid_on=None, status="published", import_id=None
    )
