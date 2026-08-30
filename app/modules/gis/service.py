"""Business rules of the spatial core. Public surface for levels 3+ (norms 3.7,
applications 3.9): published_version(), list_contours(), run_checks()."""

import json
import math
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.abac import Zone, zone_filter, zone_of
from app.core.errors import DomainError, err
from app.core.models import MediaFile
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import Organization
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth.models import User
from app.modules.gis import checks, repo
from app.modules.gis.models import Contour, ContourVersion, GisImport, GisLayer, LayerFeature


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
    """
    await _assert_in_zone(db, actor, organization_id)
    if parent_id is not None and kind != "subcontour":
        raise err("ERR-VAL-001", details={"reason": "parent_needs_subcontour"})
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
        # The session is poisoned after this (same reasoning as create_version's
        # DBAPIError below) — raise immediately, touch `db` no further on this
        # path; get_db's rollback-on-exception clears the aborted transaction.
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
    db: AsyncSession, contour_id: uuid.UUID, *, actor: User, status: str | None = None
) -> Contour:
    """`PATCH /gis/contours/{id}` — identity-level housekeeping only (today,
    archiving). Geometry changes always go through a new version, never through
    this route."""
    contour = await repo.contour_by_id(db, contour_id)
    if contour is None:
        raise err("ERR-SYS-003")
    await _assert_in_zone(db, actor, contour.organization_id)
    before = {"status": contour.status}
    if status is not None:
        contour.status = status
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
        new_value={"status": contour.status},
    )
    return contour


async def create_version(
    db: AsyncSession, contour_id: uuid.UUID, *, actor: Any, **fields: Any
) -> ContourVersion:
    contour = await repo.contour_by_id(db, contour_id)
    if contour is None:
        raise err("ERR-SYS-003")
    await _assert_in_zone(db, actor, contour.organization_id)
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


async def update_version(
    db: AsyncSession, contour_id: uuid.UUID, version_id: uuid.UUID, *, actor: User, **fields: Any
) -> ContourVersion:
    """`PATCH /gis/contours/{id}/versions/{vid}` — draft only; anything else is a
    409 (a published/archived/etc. version is a fact of record, not editable)."""
    contour = await repo.contour_by_id(db, contour_id)
    if contour is None:
        raise err("ERR-SYS-003")
    await _assert_in_zone(db, actor, contour.organization_id)
    version = await db.get(ContourVersion, version_id)
    if version is None or version.contour_id != contour_id:
        raise err("ERR-SYS-003")
    if version.status != "draft":
        raise err("ERR-GIS-005", details={"reason": "not_draft"})
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
# `TRANSITIONS` lists every edge of tz/07's lifecycle, including two this task
# has no endpoint for yet (`review` -> `draft`, `approved` -> `review`: sending
# work back for rework) — the table describes the full state graph even where
# only four of its edges are reachable through an HTTP route today.

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
    check suite (Task 4) runs at publish, not here."""
    version, contour = await _version_and_contour(db, version_id)
    await _assert_in_zone(db, actor, contour.organization_id)
    _assert_transition(version, "review")
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
    if layer is None or layer.code != "contours":
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
    version."""
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
    _assert_batch_not_empty(versions)
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
    version is simply absent from the next `status='approved'` batch.
    """
    row = await _load_import_for_transition(db, import_id, actor=actor, expected_status="approved")
    versions = await repo.import_versions(db, import_id, status="approved")
    _assert_batch_not_empty(versions)
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
OccupancyProvider = Callable[[AsyncSession, uuid.UUID], Awaitable[Decimal]]
OCCUPANCY_PROVIDERS: list[OccupancyProvider] = []


async def occupancy_ha(db: AsyncSession, contour_id: uuid.UUID) -> tuple[Decimal, str]:
    """Sum every registered provider's answer for one contour. `Decimal`,
    never `float` (project convention: areas are numeric) — quantized to 4 dp
    to match `contour_versions.area_ha`'s own NUMERIC(12,4) scale, the figure
    `s_available_ha` is subtracted against. With no provider registered the
    source is `"none"` and the figure is an explicit `Decimal('0.0000')`, so a
    reader can never mistake this placeholder for a measurement; once
    something registers, the source flips to `"permits"` (ruling 14)."""
    if not OCCUPANCY_PROVIDERS:
        return Decimal("0.0000"), "none"
    total = Decimal("0")
    for provider in OCCUPANCY_PROVIDERS:
        total += await provider(db, contour_id)
    return total.quantize(Decimal("0.0001")), "permits"


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
    actor: User,
) -> list[dict[str, Any]]:
    """`GET /gis/contours` — picking a plot (design/03, C3 item 3). Joins to
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
    rows = await repo.list_contours(
        db, organization_id=organization_id, bbox=parsed_bbox, zone=zone
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        occupied, source = await occupancy_ha(db, row.contour_id)
        items.append(
            {
                "id": row.contour_id,
                "number": row.number,
                "organization_id": row.organization_id,
                "area_ha": row.area_ha,
                "occupied_ha": occupied,
                "s_available_ha": row.area_ha - occupied,
                "occupancy_source": source,
            }
        )
    return items


async def contour_card(db: AsyncSession, contour_id: uuid.UUID, *, actor: User) -> dict[str, Any]:
    """`GET /gis/contours/{id}` — the published version's geometry plus the
    same occupancy placeholder `list_contours` carries (ruling 14). Requires a
    published version to exist, for every role alike (mirrors `list_contours`'
    own join) — a contour that never reached `published` has nothing here yet
    to show as its card."""
    row = await repo.contour_card(db, contour_id)
    if row is None:
        raise err("ERR-SYS-003")
    occupied, source = await occupancy_ha(db, contour_id)
    return {
        "id": row.contour_id,
        "number": row.number,
        "organization_id": row.organization_id,
        "kind": row.kind,
        "version_id": row.version_id,
        "area_ha": row.area_ha,
        "geometry": json.loads(row.geometry),
        "occupied_ha": occupied,
        "s_available_ha": row.area_ha - occupied,
        "occupancy_source": source,
    }


async def list_features(
    db: AsyncSession,
    code: str,
    *,
    bbox: str | None = None,
    valid_on: date | None = None,
    actor: User,
) -> dict[str, Any]:
    """`GET /gis/layers/{code}/features` — the GeoJSON a map draws (ruling 5).
    A non-public layer is refused to an actor whose ROLE is `applicant`
    specifically — not a permission gate (every staff role reads any layer's
    features regardless of a held grant, the same way `GET /gis/layers`
    itself is open to any authenticated user, ruling 18)."""
    layer = await repo.layer_by_code(db, code)
    if layer is None:
        raise err("ERR-SYS-003")
    if not layer.is_public:
        role = await auth_repo.role_code(db, actor)
        if role == "applicant":
            raise err("ERR-ACL-001")
    parsed_bbox = _parse_bbox(bbox)
    return await repo.features_geojson(db, layer_code=code, bbox=parsed_bbox, valid_on=valid_on)
