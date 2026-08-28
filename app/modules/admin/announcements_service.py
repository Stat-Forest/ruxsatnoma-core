"""Announcements service (tz/02: admin module owns them): audience-targeted admin
CRUD plus the visibility-filtered reader. `announcement_grants_access` plugs into
`app.core.files.ACCESS_CHECKS` at import time (bottom of this module) — a file
attached to an announcement the caller can currently see becomes readable through
`GET /files/{id}` too (Task 8 ruling 5).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files
from app.core.errors import err
from app.core.schemas import LocalizedName, Page, PageParams
from app.modules.admin import repo
from app.modules.admin.models import Announcement
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth.models import User


class AudienceIn(BaseModel):
    """Targeting rule for `Announcement.audience` jsonb; `region_ids` are stored as
    strings (design/02: jsonb keys/elements are text, not native uuid)."""

    role_codes: list[str] | None = None
    region_ids: list[uuid.UUID] | None = None


class FileRef(BaseModel):
    id: uuid.UUID
    filename: str
    content_type: str


class AnnouncementOut(BaseModel):
    """Reader shape (`GET /announcements`, `GET /announcements/{id}`) — no
    `status`/`audience`/`created_by`: those are admin-only internals, and a row only
    ever reaches this shape already visibility-filtered."""

    id: uuid.UUID
    title: dict[str, Any]
    body: dict[str, Any]
    publish_from: datetime | None
    publish_to: datetime | None
    files: list[FileRef]


class AnnouncementAdminOut(BaseModel):
    id: uuid.UUID
    title: dict[str, Any]
    body: dict[str, Any]
    audience: dict[str, Any] | None
    status: str
    publish_from: datetime | None
    publish_to: datetime | None
    files: list[FileRef]
    created_by: uuid.UUID
    created_at: datetime


class AnnouncementCreateIn(BaseModel):
    title: LocalizedName
    body: LocalizedName
    audience: AudienceIn | None = None
    publish_from: datetime | None = None
    publish_to: datetime | None = None
    file_ids: list[uuid.UUID] | None = None


class AnnouncementPatchIn(BaseModel):
    """All fields optional — only keys present in the request are touched
    (`exclude_unset=True`), same convention as `OrganizationPatch`. `file_ids`, when
    present, replaces the whole attached set."""

    title: LocalizedName | None = None
    body: LocalizedName | None = None
    audience: AudienceIn | None = None
    publish_from: datetime | None = None
    publish_to: datetime | None = None
    file_ids: list[uuid.UUID] | None = None


def _audience_dict(aud: AudienceIn | None) -> dict[str, Any] | None:
    """`None`/absent fields are dropped, not stored as JSON `null` — a stored
    `null` at a key would fail `.has_key()`'s "no restriction" fast path in
    `announcement_visibility_clause` (only a fully missing key or a NULL `audience`
    column take it)."""
    if aud is None:
        return None
    data: dict[str, Any] = {}
    if aud.role_codes is not None:
        data["role_codes"] = aud.role_codes
    if aud.region_ids is not None:
        data["region_ids"] = [str(region_id) for region_id in aud.region_ids]
    return data or None


_AUDITED_FIELDS = ("title", "body", "audience", "publish_from", "publish_to", "status")


def _snapshot(ann: Announcement) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for field in _AUDITED_FIELDS:
        value = getattr(ann, field)
        data[field] = value.isoformat() if isinstance(value, datetime) else value
    return data


async def _announcement_or_404(db: AsyncSession, announcement_id: uuid.UUID) -> Announcement:
    ann = await repo.get_announcement(db, announcement_id)
    if ann is None:
        raise err("ERR-SYS-003", details={"announcement": str(announcement_id)})
    return ann


def _dedupe(file_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Order-preserving de-dup — `AnnouncementFile`'s PK is `(announcement_id,
    file_id)`, so a repeated id in the request would otherwise crash the replace-set
    insert on a duplicate-key violation instead of just being attached once."""
    return list(dict.fromkeys(file_ids))


async def _validate_file_ids(db: AsyncSession, file_ids: list[uuid.UUID]) -> None:
    if not file_ids:
        return
    count = await repo.count_active_media_files(db, file_ids)
    if count != len(file_ids):
        raise err("ERR-VAL-001", details={"reason": "file_not_found"})


async def _to_reader_out(db: AsyncSession, ann: Announcement) -> AnnouncementOut:
    attached = await repo.list_announcement_files(db, ann.id)
    return AnnouncementOut(
        id=ann.id,
        title=ann.title,
        body=ann.body,
        publish_from=ann.publish_from,
        publish_to=ann.publish_to,
        files=[
            FileRef(id=f.id, filename=f.filename, content_type=f.content_type) for f in attached
        ],
    )


async def _to_admin_out(db: AsyncSession, ann: Announcement) -> AnnouncementAdminOut:
    attached = await repo.list_announcement_files(db, ann.id)
    return AnnouncementAdminOut(
        id=ann.id,
        title=ann.title,
        body=ann.body,
        audience=ann.audience,
        status=ann.status,
        publish_from=ann.publish_from,
        publish_to=ann.publish_to,
        files=[
            FileRef(id=f.id, filename=f.filename, content_type=f.content_type) for f in attached
        ],
        created_by=ann.created_by,
        created_at=ann.created_at,
    )


# --- Reader: audience-filtered, published, in-window --------------------------------


async def list_public(db: AsyncSession, *, params: PageParams, user: User) -> Page[AnnouncementOut]:
    role = await auth_repo.role_code(db, user)
    assert role is not None  # FK guarantees a role row
    rows, total = await repo.list_visible_announcements(
        db, role_code=role, region_id=user.region_id, offset=params.offset, limit=params.page_size
    )
    items = [await _to_reader_out(db, row) for row in rows]
    return Page[AnnouncementOut](
        items=items, total=total, page=params.page, page_size=params.page_size
    )


async def get_public(
    db: AsyncSession, *, announcement_id: uuid.UUID, user: User
) -> AnnouncementOut:
    role = await auth_repo.role_code(db, user)
    assert role is not None
    ann = await repo.get_visible_announcement(
        db, announcement_id, role_code=role, region_id=user.region_id
    )
    if ann is None:
        raise err("ERR-SYS-003", details={"announcement": str(announcement_id)})
    return await _to_reader_out(db, ann)


# --- Admin CRUD -----------------------------------------------------------------------


async def list_admin(
    db: AsyncSession, *, params: PageParams, status: str | None
) -> Page[AnnouncementAdminOut]:
    rows, total = await repo.list_admin_announcements(
        db, status=status, offset=params.offset, limit=params.page_size
    )
    items = [await _to_admin_out(db, row) for row in rows]
    return Page[AnnouncementAdminOut](
        items=items, total=total, page=params.page, page_size=params.page_size
    )


async def get_admin(db: AsyncSession, *, announcement_id: uuid.UUID) -> AnnouncementAdminOut:
    ann = await _announcement_or_404(db, announcement_id)
    return await _to_admin_out(db, ann)


async def create(
    db: AsyncSession, *, data: AnnouncementCreateIn, actor: User
) -> AnnouncementAdminOut:
    file_ids = _dedupe(data.file_ids or [])
    await _validate_file_ids(db, file_ids)
    ann = Announcement(
        title=data.title.root,
        body=data.body.root,
        audience=_audience_dict(data.audience),
        publish_from=data.publish_from,
        publish_to=data.publish_to,
        created_by=actor.id,
    )
    await repo.add(db, ann)
    if file_ids:
        await repo.set_announcement_files(db, ann.id, file_ids)
    await audit.log(
        db,
        action="announcement.create",
        user_id=actor.id,
        object_type="announcement",
        object_id=ann.id,
        new_value=_snapshot(ann),
    )
    return await _to_admin_out(db, ann)


async def patch(
    db: AsyncSession, *, announcement_id: uuid.UUID, data: AnnouncementPatchIn, actor: User
) -> AnnouncementAdminOut:
    ann = await _announcement_or_404(db, announcement_id)
    if ann.status == "archived":
        raise err("ERR-VAL-001", details={"reason": "archived"})
    fields = data.model_dump(exclude_unset=True)
    before = _snapshot(ann)

    if "title" in fields and data.title is not None:
        ann.title = data.title.root
    if "body" in fields and data.body is not None:
        ann.body = data.body.root
    if "audience" in fields:
        ann.audience = _audience_dict(data.audience)
    if "publish_from" in fields:
        ann.publish_from = fields["publish_from"]
    if "publish_to" in fields:
        ann.publish_to = fields["publish_to"]
    if "file_ids" in fields:
        file_ids = _dedupe(fields["file_ids"] or [])
        await _validate_file_ids(db, file_ids)
        await repo.set_announcement_files(db, ann.id, file_ids)

    await db.flush()
    await audit.log(
        db,
        action="announcement.update",
        user_id=actor.id,
        object_type="announcement",
        object_id=ann.id,
        old_value=before,
        new_value=_snapshot(ann),
    )
    return await _to_admin_out(db, ann)


async def publish(
    db: AsyncSession, *, announcement_id: uuid.UUID, actor: User
) -> AnnouncementAdminOut:
    ann = await _announcement_or_404(db, announcement_id)
    if ann.status == "archived":
        raise err("ERR-VAL-001", details={"reason": "archived"})
    if ann.status == "published":
        raise err("ERR-VAL-001", details={"reason": "already_published"})
    before = _snapshot(ann)
    if ann.publish_from is None:
        ann.publish_from = datetime.now(UTC)
    if ann.publish_to is not None and ann.publish_to <= ann.publish_from:
        raise err("ERR-VAL-001", details={"reason": "bad_window"})
    ann.status = "published"
    await db.flush()
    await audit.log(
        db,
        action="announcement.publish",
        user_id=actor.id,
        object_type="announcement",
        object_id=ann.id,
        old_value=before,
        new_value=_snapshot(ann),
    )
    return await _to_admin_out(db, ann)


async def archive(
    db: AsyncSession, *, announcement_id: uuid.UUID, actor: User
) -> AnnouncementAdminOut:
    ann = await _announcement_or_404(db, announcement_id)
    if ann.status != "archived":
        before = _snapshot(ann)
        ann.status = "archived"
        await db.flush()
        await audit.log(
            db,
            action="announcement.archive",
            user_id=actor.id,
            object_type="announcement",
            object_id=ann.id,
            old_value=before,
            new_value=_snapshot(ann),
        )
    return await _to_admin_out(db, ann)


# --- Files-access grant (ruling 5): plugs into app.core.files.ACCESS_CHECKS ---------


async def announcement_grants_access(db: AsyncSession, file_id: uuid.UUID, user: Any) -> bool:
    """A stranger can `GET /files/{id}` when the file is attached to an
    announcement they can currently see — same visibility rule the reader routes
    use, so archiving/un-targeting the announcement revokes it again."""
    role = await auth_repo.role_code(db, user)
    if role is None:
        return False
    return await repo.file_visible_via_announcement(
        db, file_id, role_code=role, region_id=user.region_id
    )


# Import-time registration — mirrors admin/permissions.py's register-on-import idiom
# (files.py's own docstring: "admin appends a checker that opens files attached to
# announcements the caller can see"). Triggered as soon as this module is imported,
# which happens when announcements_router.py (imported by app/main.py) imports it.
files.ACCESS_CHECKS.append(announcement_grants_access)
