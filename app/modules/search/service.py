"""`search`'s business logic: zone-scoped cross-entity search, CRUD for a
user's own saved search profiles, and the С22 watermarked export.

**The one rule this module must not break** (plan header): every list here
ANDs `abac.zone_filter` onto the query before anything else runs — a
republic-wide actor (`leadership`, `central_admin`, `prosecutor`) gets
`true()` and sees everything, a leshoz-scoped one is restricted to their own
organization, and there is no third path. `search_applications`/
`search_permits` below build that predicate the exact way
`permits.service.list_permits` already does for its own table; see
`docs/plans/04.5-4.7-search-archive.md` ruling 2 for why the two kinds are
two functions rather than one UNION query. `_rows_for` is the ONE place
`kind` dispatches to its own repo call — `search()` (paged, page_size<=100)
and `create_export()` (a single page up to the configured cap) both go
through it, so the export can never reach a row the screen's own zone filter
would have hidden (С22 track brief's central constraint)."""

import uuid
from typing import Any

from sqlalchemy import Row, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files, settings_store, storage
from app.core.abac import Zone, zone_filter, zone_of
from app.core.errors import err
from app.core.models import MediaFile
from app.core.schemas import Page, PageParams
from app.core.time import business_today
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.permits.models import Permit
from app.modules.search import render, repo
from app.modules.search.models import ExportJob, SavedFilter
from app.modules.search.schemas import (
    ExportCreate,
    SavedFilterIn,
    SavedFilterPatch,
    SearchKind,
    SearchResultOut,
)

PROFILE_CREATE = "search.profile_create"
PROFILE_UPDATE = "search.profile_update"
PROFILE_DELETE = "search.profile_delete"
EXPORT_CREATE = "search.export"

# `files.save_upload`'s default MIME table is built for citizen/staff INGEST
# and has no XLSX entry at all; a caller with its own ingest policy passes
# its own table instead of widening that whitelist for every uploader in the
# system (plan 03.6a ruling 8 — `gis`'s geodata import is the precedent).
_EXPORT_CONTENT_TYPES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (b"PK\x03\x04",),
}
EXPORT_MEDIA_TYPE = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _application_scope(zone: Zone, contour_organization_col: Any) -> Any:
    """`organization_col` is the application's EFFECTIVE organization
    (`assigned_org_id`, or the contour's owner while still unassigned) —
    never `Application.assigned_org_id` alone, matching `applications.
    service.list_applications`'s own documented reasoning: `assigned_org_id`
    is null for every DRAFT and stays null through SUBMITTED, so scoping on
    it alone hid a zone-scoped searcher's own leshoz's unassigned
    applications with no error and no signal (seam audit, 2026-09-06)."""
    return zone_filter(
        zone,
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=func.coalesce(Application.assigned_org_id, contour_organization_col),
    )


def _permit_scope(zone: Zone) -> Any:
    return zone_filter(
        zone,
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Permit.organization_id,
    )


async def _rows_for(
    db: AsyncSession,
    *,
    actor: User,
    kind: SearchKind,
    q: str | None,
    status: str | None,
    organization_id: uuid.UUID | None,
    activity_type_id: uuid.UUID | None,
    series: str | None,
    offset: int,
    limit: int,
) -> tuple[list[Row], int]:
    """Zone-scoped rows for one `kind`, shared by `search()` and
    `create_export()` — see this module's own docstring for why that sharing
    is the point, not an optimization."""
    zone = zone_of(actor)
    if kind == "applications":
        # Built here and handed down as an expression, not called from the
        # repo: cross-module calls live in the service layer
        # (`applications.service.list_applications`'s own review I2).
        contour_organization_col = gis_service.contour_organization_column(Application.contour_id)
        return await repo.search_applications(
            db,
            scope=_application_scope(zone, contour_organization_col),
            contour_organization_col=contour_organization_col,
            q=q,
            status=status,
            organization_id=organization_id,
            activity_type_id=activity_type_id,
            offset=offset,
            limit=limit,
        )
    return await repo.search_permits(
        db,
        scope=_permit_scope(zone),
        q=q,
        status=status,
        organization_id=organization_id,
        series=series,
        offset=offset,
        limit=limit,
    )


async def search(
    db: AsyncSession,
    *,
    actor: User,
    kind: SearchKind,
    params: PageParams,
    q: str | None = None,
    status: str | None = None,
    organization_id: uuid.UUID | None = None,
    activity_type_id: uuid.UUID | None = None,
    series: str | None = None,
) -> Page[SearchResultOut]:
    """`GET /search`. `kind` has already been validated as a `SearchKind`
    literal by the router's schema — a value outside `models.SEARCH_KINDS`
    cannot reach here."""
    rows, total = await _rows_for(
        db,
        actor=actor,
        kind=kind,
        q=q,
        status=status,
        organization_id=organization_id,
        activity_type_id=activity_type_id,
        series=series,
        offset=params.offset,
        limit=params.page_size,
    )
    items = [
        SearchResultOut(
            kind=kind,
            id=row.id,
            number=row.number,
            status=row.status,
            organization_id=(
                row.assigned_org_id if kind == "applications" else row.organization_id
            ),
            applicant_name=row.applicant_name,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return Page(items=items, total=total, page=params.page, page_size=params.page_size)


# --- export (С22) ---------------------------------------------------------


def _export_params(data: ExportCreate) -> dict[str, Any]:
    """`ExportJob.params` — the exact filter the operator ran, frozen for
    later evidence (`models.ExportJob`'s own docstring). UUIDs are stringified
    once here rather than trusted to a JSONB bind (`.claude/lessons.md`
    "Nothing in this app configures a JSON encoder")."""
    return {
        "q": data.q,
        "status": data.status,
        "organization_id": str(data.organization_id) if data.organization_id else None,
        "activity_type_id": str(data.activity_type_id) if data.activity_type_id else None,
        "series": data.series,
    }


async def create_export(db: AsyncSession, actor: User, data: ExportCreate) -> ExportJob:
    """С22: one PDF/XLSX file per call, rendered and stored SYNCHRONOUSLY —
    `models.py`'s own docstring justifies the table's name against that
    choice. `_rows_for` is called with the actor's own zone exactly as
    `search()` calls it, so the export can never widen what the screen
    already allows; the cap is enforced by `limit`, and the gap between
    `row_count` and `total_matched` (if any) is what makes a truncation
    visible rather than silent."""
    cap = await settings_store.get_int(db, "search_export_max_rows")
    rows, total = await _rows_for(
        db,
        actor=actor,
        kind=data.kind,
        q=data.q,
        status=data.status,
        organization_id=data.organization_id,
        activity_type_id=data.activity_type_id,
        series=data.series,
        offset=0,
        limit=cap,
    )

    watermark = render.watermark_text(actor.full_name, business_today())
    content = (
        render.render_pdf(rows, kind=data.kind, watermark=watermark)
        if data.format == "pdf"
        else render.render_xlsx(rows, kind=data.kind, watermark=watermark)
    )

    document = await files.save_upload(
        db,
        data=content,
        filename=f"export-{data.kind}-{uuid.uuid4().hex[:8]}.{data.format}",
        content_type=EXPORT_MEDIA_TYPE[data.format],
        actor=actor,
        allowed=_EXPORT_CONTENT_TYPES,
    )

    job = ExportJob(
        user_id=actor.id,
        kind=data.kind,
        format=data.format,
        params=_export_params(data),
        status="done",
        file_id=document.id,
        row_count=len(rows),
        total_matched=total,
        watermarked=True,
    )
    await repo.create_export_job(db, job)
    await audit.log(
        db,
        action=EXPORT_CREATE,
        user_id=actor.id,
        object_type="export_job",
        object_id=job.id,
        new_value={
            "kind": data.kind,
            "format": data.format,
            "row_count": job.row_count,
            "total_matched": job.total_matched,
        },
    )
    # `created_at`/`finished_at` are DB-computed (`server_default=func.now()`)
    # and stay expired in memory after a plain flush — the exact shape
    # `.claude/lessons.md` "A row in memory is not what Postgres stored"
    # warns about for a caller that both creates AND returns such a row.
    await db.refresh(job)
    return job


async def list_export_jobs(db: AsyncSession, actor: User) -> list[ExportJob]:
    return await repo.list_export_jobs(db, user_id=actor.id)


async def get_export_job(db: AsyncSession, actor: User, job_id: uuid.UUID) -> ExportJob:
    """Owner-only (`saved_filters`' own non-owner path returns ERR-SYS-003,
    never ERR-ACL-001 — hiding EXISTENCE, not merely access, is this
    project's convention for a private row: `get_saved_filter`'s own
    docstring precedent). An export is a private working file handed to
    someone outside the screen by the person who ran it, never a profile
    peers browse."""
    job = await repo.export_job_by_id(db, job_id)
    if job is None or job.user_id != actor.id:
        raise err("ERR-SYS-003")
    return job


async def get_export_file(
    db: AsyncSession, actor: User, job_id: uuid.UUID
) -> tuple[ExportJob, bytes]:
    """The stored bytes exactly as rendered — never a re-render (same
    reasoning `permits.doc_hash` is frozen at issuance for: the evidentiary
    point of this table is that a re-fetch answers with what was ACTUALLY
    handed over, not a fresh query against data that may have since
    changed)."""
    job = await get_export_job(db, actor, job_id)
    if job.file_id is None:
        raise err("ERR-SYS-003")
    file = await db.get(MediaFile, job.file_id)
    if file is None:
        raise err("ERR-SYS-003")
    data = await storage.get_object(file.storage_key)
    return job, data


async def create_saved_filter(db: AsyncSession, actor: User, data: SavedFilterIn) -> SavedFilter:
    row = SavedFilter(
        user_id=actor.id, name=data.name, kind=data.kind, params=data.params, shared=data.shared
    )
    try:
        async with db.begin_nested():
            await repo.create_saved_filter(db, row)
    except IntegrityError as exc:
        cause = exc.orig.__cause__ if exc.orig is not None else None
        if getattr(cause, "constraint_name", None) != "uq_saved_filters_user_name":
            raise
        raise err("ERR-SRCH-001", details={"reason": "name_taken"}) from exc
    await audit.log(
        db, action=PROFILE_CREATE, user_id=actor.id, object_type="saved_filter", object_id=row.id
    )
    return row


async def list_saved_filters(db: AsyncSession, actor: User) -> list[SavedFilter]:
    # `role_code` is `str | None` only for a role row vanished behind an FK
    # (auth.repo's own docstring: "should not happen") — "" never matches a
    # real role code, so the `None` case just means "own profiles only".
    role_code = await auth_repo.role_code(db, actor) or ""
    return await repo.list_saved_filters(db, user_id=actor.id, role_code=role_code)


def _visible(row: SavedFilter, actor: User, role_code: str) -> bool:
    if row.user_id == actor.id:
        return True
    shared = row.shared or {}
    return role_code in shared.get("role_codes", []) or str(actor.id) in shared.get("user_ids", [])


async def get_saved_filter(db: AsyncSession, actor: User, filter_id: uuid.UUID) -> SavedFilter:
    row = await repo.saved_filter_by_id(db, filter_id)
    role_code = await auth_repo.role_code(db, actor) or ""
    if row is None or not _visible(row, actor, role_code):
        raise err("ERR-SYS-003")
    return row


async def update_saved_filter(
    db: AsyncSession, actor: User, filter_id: uuid.UUID, patch: SavedFilterPatch
) -> SavedFilter:
    """Owner-only — being able to SEE a shared profile does not mean being
    able to change it for everyone it is shared with."""
    row = await repo.saved_filter_by_id(db, filter_id)
    if row is None:
        raise err("ERR-SYS-003")
    if row.user_id != actor.id:
        raise err("ERR-ACL-001", details={"permission": "owner"})
    if patch.name is not None:
        row.name = patch.name
    if patch.params is not None:
        row.params = patch.params
    if "shared" in patch.model_fields_set:
        row.shared = patch.shared
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        cause = exc.orig.__cause__ if exc.orig is not None else None
        if getattr(cause, "constraint_name", None) != "uq_saved_filters_user_name":
            raise
        raise err("ERR-SRCH-001", details={"reason": "name_taken"}) from exc
    await audit.log(
        db, action=PROFILE_UPDATE, user_id=actor.id, object_type="saved_filter", object_id=row.id
    )
    return row


async def delete_saved_filter(db: AsyncSession, actor: User, filter_id: uuid.UUID) -> None:
    row = await repo.saved_filter_by_id(db, filter_id)
    if row is None:
        raise err("ERR-SYS-003")
    if row.user_id != actor.id:
        raise err("ERR-ACL-001", details={"permission": "owner"})
    await repo.delete_saved_filter(db, row)
    await audit.log(
        db,
        action=PROFILE_DELETE,
        user_id=actor.id,
        object_type="saved_filter",
        object_id=filter_id,
    )
