"""`search`'s business logic: zone-scoped cross-entity search, and CRUD for a
user's own saved search profiles.

**The one rule this module must not break** (plan header): every list here
ANDs `abac.zone_filter` onto the query before anything else runs — a
republic-wide actor (`leadership`, `central_admin`, `prosecutor`) gets
`true()` and sees everything, a leshoz-scoped one is restricted to their own
organization, and there is no third path. `search_applications`/
`search_permits` below build that predicate the exact way
`permits.service.list_permits` already does for its own table; see
`docs/plans/04.5-4.7-search-archive.md` ruling 2 for why the two kinds are
two functions rather than one UNION query."""

import uuid
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.abac import Zone, zone_filter, zone_of
from app.core.errors import err
from app.core.schemas import Page, PageParams
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.permits.models import Permit
from app.modules.search import repo
from app.modules.search.models import SavedFilter
from app.modules.search.schemas import (
    SavedFilterIn,
    SavedFilterPatch,
    SearchKind,
    SearchResultOut,
)

PROFILE_CREATE = "search.profile_create"
PROFILE_UPDATE = "search.profile_update"
PROFILE_DELETE = "search.profile_delete"


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
    zone = zone_of(actor)
    if kind == "applications":
        # Built here and handed down as an expression, not called from the
        # repo: cross-module calls live in the service layer
        # (`applications.service.list_applications`'s own review I2).
        contour_organization_col = gis_service.contour_organization_column(Application.contour_id)
        rows, total = await repo.search_applications(
            db,
            scope=_application_scope(zone, contour_organization_col),
            contour_organization_col=contour_organization_col,
            q=q,
            status=status,
            organization_id=organization_id,
            activity_type_id=activity_type_id,
            offset=params.offset,
            limit=params.page_size,
        )
    else:
        rows, total = await repo.search_permits(
            db,
            scope=_permit_scope(zone),
            q=q,
            status=status,
            organization_id=organization_id,
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
