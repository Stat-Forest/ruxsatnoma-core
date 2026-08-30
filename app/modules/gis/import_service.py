"""The import row's state machine: claim -> parse -> write -> report (ruling 6).

Import is a JOB, not a request: `POST /gis/imports` only stores the file and
answers 202, and everything below runs later, in a worker, so a 151-feature
delivery does not need the browser to stay open. It is deliberately not the
outbox — the outbox carries messages LEAVING the system; this is inbound work.

An import is atomic and warnings are not errors (ruling 7). The whole batch's
row writes live in ONE savepoint: any error row rolls every one of them back,
while the failure record itself — `status='failed'` plus the `error_report` an
operator needs in order to fix the file — is written on the OUTER transaction
and survives. Warnings (an area mismatch, a duplicated contour number, a leshoz
name that disagrees with the request) roll nothing back; they land in
`stats.warnings` and the batch imports.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import files, settings_store, storage
from app.core.errors import DomainError, err
from app.core.models import MediaFile
from app.modules.admin import repo as admin_repo
from app.modules.audit import service as audit
from app.modules.gis import importer, repo

# `_assert_in_zone` / `_assert_approval_doc_active` are internals of the GIS
# MODULE, not of one file: this is the same `app/modules/gis` package, and
# reimplementing either here is exactly the divergence the project has been
# bitten by before. Nothing outside `gis` reaches for them.
from app.modules.gis import service as gis_service
from app.modules.gis.importer import ParsedFeature, RowError
from app.modules.gis.models import IMPORT_FORMATS, Contour, GisImport, GisLayer
from app.modules.notifications import service as notifications_service

logger = structlog.get_logger(__name__)

CREATE_ACTION = "gis_import.create"
FINISH_ACTION = "gis_import.finish"
IMPORT_EVENT = "gis.import.finished"

# The one layer whose features are contours (identity + versioned geometry);
# every other layer's features are `layer_features` rows.
CONTOUR_LAYER_CODE = "contours"

# Attribute-map keys this importer understands. Deliberately narrow for the
# contour layer (ruling 13): the Agency's file also carries tenant names and
# outstanding debts, which are personal data and must never enter the spatial
# layer — leases become permit rows in 3.11, through their own migration.
NUMBER_KEY = "number"
DECLARED_AREA_KEY = "declared_area_ha"
ORGANIZATION_NAME_KEY = "organization_name"
NAME_KEY = "name"

# NUMERIC(12,4): eight integer digits at most. A source figure that cannot fit
# is reported as a bad attribute rather than aborting the batch with a raw
# DataError halfway through the writes.
_MAX_DECLARED_AREA = Decimal(10) ** 8


class _BatchFailed(Exception):
    """Carries the rows to report out of the savepoint block that must roll
    back. A private control-flow signal, never an API error — the caller turns
    it into the import's own `error_report`."""

    def __init__(self, errors: list[RowError]) -> None:
        super().__init__(f"{len(errors)} bad row(s)")
        self.errors = errors


@dataclass(slots=True)
class _Mapped:
    """One parsed feature after `attribute_map` has been applied — everything
    the write phase needs, and nothing the source file carried that we chose not
    to store."""

    row: int
    wkb: bytes
    number: str | None = None
    declared_area_ha: Decimal | None = None
    organization_name: str | None = None
    name: str | None = None
    props: dict[str, Any] = field(default_factory=dict)


def _text(value: Any) -> str | None:
    """An attribute as trimmed text, or None when it is absent or blank. A
    shapefile writes a NULL string field as an empty string, so "missing" and
    "empty" are the same thing to us."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _map_features(
    features: list[ParsedFeature], *, attribute_map: dict[str, Any], is_contour_layer: bool
) -> tuple[list[_Mapped], list[RowError]]:
    """Apply `attribute_map` to every parsed feature, collecting a `RowError`
    per bad row rather than stopping at the first.

    Pure Python — no database — precisely so the whole file's attribute
    problems are reported in one pass. A wrong attribute map makes EVERY row
    fail, and an operator fixing a 151-row delivery must see that at once
    (tz/07: "error report — row number plus error type").
    """
    number_field = attribute_map.get(NUMBER_KEY)
    area_field = attribute_map.get(DECLARED_AREA_KEY)
    org_field = attribute_map.get(ORGANIZATION_NAME_KEY)
    name_field = attribute_map.get(NAME_KEY)
    mapped_fields = {f for f in (number_field, area_field, org_field, name_field) if f}

    mapped: list[_Mapped] = []
    errors: list[RowError] = []
    for feature in features:
        item = _Mapped(row=feature.row, wkb=feature.wkb)
        item.organization_name = _text(feature.attributes.get(org_field)) if org_field else None
        if is_contour_layer:
            item.number = _text(feature.attributes.get(number_field)) if number_field else None
            if item.number is None:
                errors.append(
                    RowError(
                        row=feature.row,
                        code="missing_attribute",
                        message=f"no contour number in field {number_field!r}",
                    )
                )
                continue
        else:
            item.name = _text(feature.attributes.get(name_field)) if name_field else None
            # Ruling 13: for a non-contour layer the UNMAPPED attributes are what
            # `props jsonb` is for. Every value is already a JSON primitive —
            # `importer._scalar` coerced the numpy scalars pyogrio returns.
            item.props = {k: v for k, v in feature.attributes.items() if k not in mapped_fields}
        if area_field:
            raw = feature.attributes.get(area_field)
            if raw is not None and _text(raw) is not None:
                try:
                    declared = Decimal(str(raw)).quantize(Decimal("0.0001"))
                # Deliberately parenthesized, not the PEP 758 bare form: for
                # years `except Foo, bar:` meant Python 2's except-as binding,
                # so the bare shape reads like that trap (same fmt:skip as
                # core.settings_store.coerce).
                except (InvalidOperation, ValueError):  # fmt: skip
                    errors.append(
                        RowError(
                            row=feature.row,
                            code="invalid_attribute",
                            message=f"{area_field!r} is not a number: {raw!r}",
                        )
                    )
                    continue
                if abs(declared) >= _MAX_DECLARED_AREA:
                    errors.append(
                        RowError(
                            row=feature.row,
                            code="invalid_attribute",
                            message=f"{area_field!r} does not fit numeric(12,4): {raw!r}",
                        )
                    )
                    continue
                item.declared_area_ha = declared
        mapped.append(item)
    return mapped, errors


def _unique_number(number: str, taken: set[str]) -> tuple[str, bool]:
    """Ruling 11: a contour number already used inside this organization gets a
    `/2`, `/3`... suffix and a `duplicate_number` warning. The Burchmulla file
    has 92 distinct numbers across 151 features — several tenants share one
    contour — and the importer deliberately does NOT guess a contour /
    sub-contour hierarchy from that: every imported feature is
    `kind='contour'`, `parent_id=NULL`, and the hierarchy is set by hand once
    the Agency delivers the contour layer itself."""
    if number not in taken:
        return number, False
    suffix = 2
    while f"{number}/{suffix}" in taken:
        suffix += 1
    return f"{number}/{suffix}", True


def _normalised_name(value: str) -> str:
    return " ".join(value.casefold().split())


async def _organization_names(db: AsyncSession, organization_id: uuid.UUID) -> set[str]:
    """Every localized name of the organization the REQUEST named, normalised
    for comparison. Read through `admin.repo`, never by re-querying
    `organizations` from this module (CLAUDE.md: reference data)."""
    org = await admin_repo.get_organization(db, organization_id)
    if org is None:
        return set()
    return {_normalised_name(v) for v in (org.name or {}).values() if isinstance(v, str)}


async def _write_batch(
    db: AsyncSession,
    row: GisImport,
    layer: GisLayer,
    mapped: list[_Mapped],
    srid: int,
    *,
    mismatch_pct: int,
) -> tuple[int, list[dict[str, Any]]]:
    """Insert every feature of one batch. Raises `_BatchFailed` on the first
    row the database refuses — the caller's savepoint then undoes everything
    this wrote, which is the whole point of running it inside one.

    Returns `(created, warnings)`.
    """
    warnings: list[dict[str, Any]] = []
    is_contour_layer = layer.code == CONTOUR_LAYER_CODE
    taken = await repo.contour_numbers(db, row.organization_id) if is_contour_layer else set()
    allowed_types = repo.GEOMETRY_TYPE_FAMILIES.get(layer.geometry_type, ())
    created = 0

    for item in mapped:
        try:
            if is_contour_layer:
                assert item.number is not None  # _map_features errored otherwise
                number, duplicated = _unique_number(item.number, taken)
                taken.add(number)
                if duplicated:
                    warnings.append(
                        {
                            "row": item.row,
                            "code": "duplicate_number",
                            "source_number": item.number,
                            "stored_number": number,
                        }
                    )
                contour = Contour(
                    layer_id=layer.id,
                    organization_id=row.organization_id,
                    number=number,
                    kind="contour",
                    created_by=row.started_by,
                )
                db.add(contour)
                await db.flush()
                version = await repo.insert_version(
                    db,
                    contour_id=contour.id,
                    version_no=1,
                    wkb=item.wkb,
                    srid=srid,
                    source="import",
                    created_by=row.started_by,
                    declared_area_ha=item.declared_area_ha,
                    approval_doc_id=row.approval_doc_id,
                    import_id=row.id,
                    status="draft",
                )
                mismatch = _area_mismatch(
                    item.row, item.declared_area_ha, version.area_ha, mismatch_pct
                )
                if mismatch is not None:
                    warnings.append(mismatch)
            else:
                geometry_type = await repo.feature_geometry_type(db, wkb=item.wkb, srid=srid)
                if geometry_type is None:
                    raise _BatchFailed(
                        [
                            RowError(
                                row=item.row,
                                code="empty_geometry",
                                message="geometry normalises to nothing",
                            )
                        ]
                    )
                if allowed_types and geometry_type not in allowed_types:
                    raise _BatchFailed(
                        [
                            RowError(
                                row=item.row,
                                code="geometry_type_mismatch",
                                message=(
                                    f"{geometry_type} is not a {layer.geometry_type} "
                                    f"for layer {layer.code!r}"
                                ),
                            )
                        ]
                    )
                await repo.insert_feature(
                    db,
                    layer_id=layer.id,
                    wkb=item.wkb,
                    srid=srid,
                    organization_id=row.organization_id,
                    created_by=row.started_by,
                    name={"ru": item.name} if item.name else None,
                    props=item.props,
                    approval_doc_id=row.approval_doc_id,
                    import_id=row.id,
                    status="draft",
                )
        except DomainError as exc:
            # `insert_version`'s ERR-GIS-001 for a geometry that normalises to
            # nothing. Inside a batch that is a bad ROW, not an API error.
            raise _BatchFailed(
                [RowError(row=item.row, code="invalid_geometry", message=exc.code)]
            ) from exc
        except DBAPIError as exc:
            # An unknown SRID, a colliding contour number from a concurrent
            # import, a value the column refuses. The savepoint is aborted now;
            # rolling it back (the caller's `async with`) is what makes the
            # session usable again for writing the report.
            raise _BatchFailed(
                [RowError(row=item.row, code="database_error", message=repr(exc.orig))]
            ) from exc
        created += 1
    return created, warnings


def _area_mismatch(
    row: int, declared: Decimal | None, computed: Decimal, mismatch_pct: int
) -> dict[str, Any] | None:
    """Ruling 2: the geometry is the area of record and the file's own figure is
    reference only — but a big disagreement is worth an operator's eye, so it is
    a WARNING. Compared against the COMPUTED area PostGIS already stored
    (`ST_Area(geom::geography)/10000`), never against anything recomputed here.
    36 of Burchmulla's 151 features trip this; blocking on it would stop the
    delivery dead."""
    if declared is None or declared <= 0:
        return None
    if abs(computed - declared) / declared * 100 <= mismatch_pct:
        return None
    return {
        "row": row,
        "code": "area_mismatch",
        # float(), not Decimal: `stats` is JSONB written through the stock
        # json.dumps, which has no encoder for Decimal (lesson).
        "declared_ha": float(declared),
        "computed_ha": float(computed),
    }


async def _load_file(db: AsyncSession, row: GisImport) -> bytes | None:
    """The uploaded bytes back out of MinIO. `None` means the object is gone (a
    wiped bucket, a lifecycle rule): the import is unrunnable, which is a
    `failed` record — not an exception that would make the job raise once per
    tick, forever, on the same row."""
    file = await db.get(MediaFile, row.file_id)
    if file is None:
        return None
    try:
        return await storage.get_object(file.storage_key)
    except FileNotFoundError:
        return None


async def _finish(
    db: AsyncSession,
    row: GisImport,
    layer_code: str,
    *,
    status: str,
    stats: dict[str, Any] | None,
    errors: list[RowError] | None,
) -> None:
    """The one exit of every import, successful or not: stamp the row, tell the
    initiator, audit. A failed import notifies too — a 20-minute import must not
    need the browser to stay open (ruling 6), and that is just as true when it
    fails."""
    row.status = status
    row.stats = stats
    row.error_report = [e.as_dict() for e in errors] if errors else None
    row.finished_at = datetime.now(UTC)
    await db.flush()
    correlation = f"job:{uuid.uuid4()}"
    if row.started_by is not None:
        await notifications_service.notify(
            db,
            event_code=IMPORT_EVENT,
            recipient_user_id=row.started_by,
            params={
                "layer": layer_code,
                "created": (stats or {}).get("created", 0),
                "status": status,
            },
            object_type="gis_import",
            object_id=row.id,
            correlation_id=correlation,
        )
    else:
        # Nothing to notify: a seeded/scripted import with no initiator. Loud in
        # the log rather than silently skipped.
        logger.warning("gis.import.no_initiator", import_id=str(row.id))
    await audit.log(
        db,
        action=FINISH_ACTION,
        user_id=None,  # a job's own action (CLAUDE.md: workers audit with user_id=None)
        object_type="gis_import",
        object_id=row.id,
        correlation_id=correlation,
        new_value={
            "status": status,
            "created": (stats or {}).get("created", 0),
            "warnings": len((stats or {}).get("warnings", [])),
            "errors": len(errors or []),
        },
    )


async def run_import(db: AsyncSession, row: GisImport) -> None:
    """Process ONE claimed import, in the caller's transaction.

    Never raises for a bad delivery: every failure a file can cause becomes
    `status='failed'` plus an `error_report`. The caller commits either way —
    the failure record is the deliverable.
    """
    row.status = "processing"
    await db.flush()

    layer = await db.get(GisLayer, row.layer_id)
    if layer is None:  # the catalogue is fixed and seeded; this cannot happen in practice
        await _finish(
            db,
            row,
            layer_code="?",
            status="failed",
            stats=None,
            errors=[RowError(row=0, code="unknown_layer", message=str(row.layer_id))],
        )
        return

    data = await _load_file(db, row)
    if data is None:
        await _finish(
            db,
            row,
            layer.code,
            status="failed",
            stats=None,
            errors=[RowError(row=0, code="file_missing", message="stored object is gone")],
        )
        return

    # GDAL is blocking C code — off the event loop, always (plan's global
    # constraints), even though this runs in a worker rather than a request.
    features, errors, srid = await asyncio.to_thread(importer.parse, data, fmt=row.format)
    mapped: list[_Mapped] = []
    if not errors:
        mapped, errors = _map_features(
            features,
            attribute_map=row.attribute_map or {},
            is_contour_layer=layer.code == CONTOUR_LAYER_CODE,
        )
    if errors:
        await _finish(db, row, layer.code, status="failed", stats=None, errors=errors)
        return

    mismatch_pct = await settings_store.get_int(db, "gis_area_mismatch_pct")
    org_names = await _organization_names(db, row.organization_id)
    try:
        # The savepoint of ruling 7: everything the batch writes is undone
        # together, while the outer transaction — and the failure record written
        # on it below — survives.
        async with db.begin_nested():
            created, warnings = await _write_batch(
                db, row, layer, mapped, srid, mismatch_pct=mismatch_pct
            )
    except _BatchFailed as exc:
        await _finish(db, row, layer.code, status="failed", stats=None, errors=exc.errors)
        return

    warnings.extend(_organization_warnings(mapped, org_names))
    await _finish(
        db,
        row,
        layer.code,
        status="review",
        stats={"created": created, "warnings": warnings},
        errors=None,
    )


def _organization_warnings(mapped: list[_Mapped], org_names: set[str]) -> list[dict[str, Any]]:
    """Ruling 12: `organization_id` comes from the REQUEST, never from the file
    — matching ~90 leshozes by a free-text Cyrillic name would fail silently and
    attach a whole batch to the wrong one. The file's own leshoz name is only
    COMPARED, and a disagreement is one warning per distinct name, not one per
    feature: 151 identical lines would bury every other warning in the report.
    """
    if not org_names:
        return []
    seen: set[str] = set()
    warnings: list[dict[str, Any]] = []
    for item in mapped:
        if item.organization_name is None:
            continue
        normalised = _normalised_name(item.organization_name)
        if normalised in org_names or normalised in seen:
            continue
        seen.add(normalised)
        warnings.append(
            {
                "row": item.row,
                "code": "organization_mismatch",
                "file_name": item.organization_name,
            }
        )
    return warnings


async def process_pending(factory: async_sessionmaker[AsyncSession]) -> int:
    """Claim and run at most one pending import. Returns how many were
    processed — 0 when the queue is empty or every due row is already locked by
    another worker."""
    async with factory() as db:
        row = await repo.claim_pending_import(db)
        if row is None:
            await db.rollback()  # release the claim transaction's snapshot promptly
            return 0
        import_id = row.id
        try:
            await run_import(db, row)
            await db.commit()
        except Exception:
            # `run_import` turns every failure a FILE can cause into a report;
            # reaching here means a defect in our own code. Roll back, then mark
            # the row failed on a fresh session — otherwise the scheduler
            # re-claims this same poisoned row every 10 seconds, forever.
            logger.exception("gis.import.crashed", import_id=str(import_id))
            await db.rollback()
            await _mark_crashed(factory, import_id)
        return 1


async def _mark_crashed(factory: async_sessionmaker[AsyncSession], import_id: uuid.UUID) -> None:
    try:
        async with factory() as db:
            row = await repo.import_by_id(db, import_id)
            if row is None:
                return
            row.status = "failed"
            row.error_report = [
                RowError(
                    row=0, code="internal_error", message="the import job failed unexpectedly"
                ).as_dict()
            ]
            row.finished_at = datetime.now(UTC)
            await db.commit()
    except Exception:
        logger.exception("gis.import.crash_marking_failed", import_id=str(import_id))


# --- The upload endpoint's own service half ----------------------------------


async def create_import(
    db: AsyncSession,
    *,
    layer_code: str,
    organization_id: uuid.UUID,
    approval_doc_id: uuid.UUID,
    fmt: str,
    attribute_map: dict[str, Any],
    data: bytes,
    filename: str,
    content_type: str,
    actor: Any,
) -> GisImport:
    """`POST /gis/imports` — store the file and QUEUE the work (ruling 6). The
    response is 202 with an id; nothing is parsed here.

    `organization_id` is zone-checked before anything else, the ordering
    `gis.service.create_contour` already documents: the leshoz comes from the
    request body, so a leshoz-scoped specialist must not be able to file an
    import against a different one. `approval_doc_id` is mandatory (ruling 3:
    one basis document per batch, stamped onto all 151 versions) and checked to
    be on record before a single byte is stored.

    The file goes through `core.files.save_upload` with gis's OWN MIME/magic
    table and cap key (ruling 8) — routing geodata through `POST /files`
    instead would mean widening the document whitelist for every uploader in
    the system.
    """
    gis_service._assert_in_zone(actor, organization_id)
    layer = await repo.layer_by_code(db, layer_code)
    if layer is None:
        raise err("ERR-SYS-003")
    if fmt not in IMPORT_FORMATS:
        raise err("ERR-GIS-004", details={"reason": "unsupported_format", "format": fmt})
    await gis_service._assert_approval_doc_active(db, approval_doc_id)
    stored = await files.save_upload(
        db,
        data=data,
        filename=filename,
        content_type=content_type,
        actor=actor,
        allowed=importer.GIS_UPLOAD_TYPES,
        cap_key="gis_import_max_mb",
    )
    row = GisImport(
        layer_id=layer.id,
        organization_id=organization_id,
        file_id=stored.id,
        approval_doc_id=approval_doc_id,
        format=fmt,
        attribute_map=attribute_map,
        status="pending",
        started_by=actor.id,
    )
    db.add(row)
    await db.flush()
    await audit.log(
        db,
        action=CREATE_ACTION,
        user_id=actor.id,
        object_type="gis_import",
        object_id=row.id,
        new_value={
            "layer_code": layer_code,
            "organization_id": str(organization_id),
            "file_id": str(stored.id),
            "approval_doc_id": str(approval_doc_id),
            "format": fmt,
            "attributes": attribute_map,
        },
    )
    return row


async def get_import(db: AsyncSession, import_id: uuid.UUID, *, actor: Any) -> GisImport:
    """`GET /gis/imports/{id}` — the batch's status, `stats` and `error_report`.
    Zone-scoped on the import's own organization, a separate gate from the
    `CONTOURS_MANAGE` permission the router applies (lesson: "Zone scoping is
    not a permission check — a read path needs both")."""
    row = await repo.import_by_id(db, import_id)
    if row is None:
        raise err("ERR-SYS-003")
    gis_service._assert_in_zone(actor, row.organization_id)
    return row
