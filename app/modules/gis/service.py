"""Business rules of the spatial core. Public surface for levels 3+ (norms 3.7,
applications 3.9): published_version(), list_contours(), run_checks()."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.abac import Zone, zone_of
from app.core.errors import err
from app.core.models import MediaFile
from app.modules.audit import service as audit
from app.modules.auth.models import User
from app.modules.gis import checks, repo
from app.modules.gis.models import Contour, ContourVersion, GisLayer, LayerFeature


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


def _assert_in_zone(actor: User, organization_id: uuid.UUID) -> None:
    """Per-request zone check on the one axis `contours` carries
    (`organization_id`) — same 'own zone' semantics as
    `admin.users_service._within_zone`, kept local since gis has no reason to
    import an admin-module internal. This is separate from the
    `CONTOURS_MANAGE` permission check the router already applies (lesson:
    "Zone scoping is not a permission check — a read path needs both", and here
    every write path needs both too): it answers WHOSE organization, not
    WHETHER the actor may manage contours at all. Takes the organization id
    directly rather than a `Contour` row — `create_contour` has no row yet when
    it needs this check (final review, finding 1: it must run before the row is
    constructed, since `organization_id` comes straight from the request body).
    `Contour` has no region_id/district_id of its own, so only the organization
    axis of the actor's zone is enforced; a region/district-scoped zone is a
    shape this stage does not assign to CONTOURS_MANAGE holders."""
    zone = zone_of(actor)
    if zone.organization_id is not None and zone.organization_id != organization_id:
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
    _assert_in_zone(actor, organization_id)
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
    _assert_in_zone(actor, contour.organization_id)
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
    _assert_in_zone(actor, contour.organization_id)
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
    _assert_in_zone(actor, contour.organization_id)
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
    _assert_in_zone(actor, contour.organization_id)
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
    _assert_in_zone(actor, contour.organization_id)
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
    _assert_in_zone(actor, contour.organization_id)
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
    _assert_in_zone(actor, contour.organization_id)
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
    _assert_in_zone(actor, contour.organization_id)
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


def _assert_feature_zone(actor: User, organization_id: uuid.UUID | None) -> None:
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
    this is exactly `_assert_in_zone`'s existing 'own organization only'
    rule, applied to every write below the same way every contour write
    already applies it."""
    if organization_id is None:
        if zone_of(actor) != Zone(None, None, None):
            raise err("ERR-ACL-001")
        return
    _assert_in_zone(actor, organization_id)


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
    _assert_feature_zone(actor, organization_id)
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
    _assert_feature_zone(actor, feature.organization_id)
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
    _assert_feature_zone(actor, feature.organization_id)
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
    _assert_feature_zone(actor, feature.organization_id)
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
