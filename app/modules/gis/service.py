"""Business rules of the spatial core. Public surface for levels 3+ (norms 3.7,
applications 3.9): published_version(), list_contours(), run_checks()."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.abac import zone_of
from app.core.errors import err
from app.modules.audit import service as audit
from app.modules.auth.models import User
from app.modules.gis import repo
from app.modules.gis.models import Contour, ContourVersion, GisLayer


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


def _assert_in_zone(actor: User, contour: Contour) -> None:
    """Per-row zone check on the one axis `contours` carries (`organization_id`)
    — same 'own zone' semantics as `admin.users_service._within_zone`, kept
    local since gis has no reason to import an admin-module internal. This is
    separate from the `CONTOURS_MANAGE` permission check the router already
    applies (lesson: "Zone scoping is not a permission check — a read path needs
    both", and here every write path needs both too): it answers WHOSE contour,
    not WHETHER the actor may manage contours at all. `Contour` has no
    region_id/district_id of its own, so only the organization axis of the
    actor's zone is enforced; a region/district-scoped zone is a shape this
    stage does not assign to CONTOURS_MANAGE holders."""
    zone = zone_of(actor)
    if zone.organization_id is not None and zone.organization_id != contour.organization_id:
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
    """
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
    _assert_in_zone(actor, contour)
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
    _assert_in_zone(actor, contour)
    version_no = await repo.next_version_no(db, contour_id)
    try:
        version = await repo.insert_version(
            db, contour_id=contour_id, version_no=version_no, created_by=actor.id, **fields
        )
    except DBAPIError as exc:  # malformed GeoJSON reaches us as a PostGIS error
        # ST_GeomFromGeoJSON's XX000 poisons the session the same way the
        # IntegrityError above does — raise immediately and nothing else touches
        # `db` on this path; get_db's except-and-rollback (app/core/deps.py) is
        # what actually clears the aborted transaction before this becomes a
        # response. Do not add an audit call here: it would run inside the still
        # -aborted transaction and fail a second time.
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
    _assert_in_zone(actor, contour)
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
