"""Legal-documents service (`0043`): the register behind `landing`'s /documents
page — an admin CRUD with the announcements lifecycle, and an anonymous surface
the public site reads.

Two things here are NOT copies of `announcements_service`:

* `publish` refuses a row with neither an attached file nor a `source_url`.
  The page this register replaces printed four titles under a "Download PDF"
  button wired to nothing; publishing a row that opens nothing would rebuild
  that page out of real data (plan 07.8 ruling R3).
* there is no publication window and no audience. A law is not addressed at a
  role or a region, and it does not stop applying on a date an editor picks in
  advance — so `status` alone decides what the internet sees.
"""

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.errors import err
from app.core.models import MediaFile
from app.core.schemas import LocalizedName, Page, PageParams
from app.modules.admin import repo

# `FileRef` is the same three fields an announcement's attachment exposes, and
# deliberately the same schema: splitting it in two would give the OpenAPI
# contract (and every generated client) two names for one thing.
from app.modules.admin.announcements_service import FileRef
from app.modules.admin.models import LegalDocument
from app.modules.audit import service as audit
from app.modules.auth.models import User


class LegalDocumentOut(BaseModel):
    """What the anonymous site gets. No `status`, no `sort_order`, no
    `created_by`: those are editorial bookkeeping, and the citizen gets what the
    page prints. `file` is null for a document that lives only on lex.uz."""

    id: uuid.UUID
    title: dict[str, Any]
    summary: dict[str, Any] | None
    doc_number: str
    adopted_on: date
    source_url: str | None
    file: FileRef | None


class LegalDocumentAdminOut(BaseModel):
    id: uuid.UUID
    title: dict[str, Any]
    summary: dict[str, Any] | None
    doc_number: str
    adopted_on: date
    source_url: str | None
    file: FileRef | None
    status: str
    sort_order: int
    created_by: uuid.UUID
    created_at: datetime


class LegalDocumentCreateIn(BaseModel):
    title: LocalizedName
    summary: LocalizedName | None = None
    doc_number: str
    adopted_on: date
    source_url: str | None = None
    file_id: uuid.UUID | None = None
    sort_order: int = 0


class LegalDocumentPatchIn(BaseModel):
    """All fields optional — only keys present in the request are touched
    (`exclude_unset=True`), the convention `AnnouncementPatchIn` established."""

    title: LocalizedName | None = None
    summary: LocalizedName | None = None
    doc_number: str | None = None
    adopted_on: date | None = None
    source_url: str | None = None
    file_id: uuid.UUID | None = None
    sort_order: int | None = None


_AUDITED_FIELDS = (
    "title",
    "summary",
    "doc_number",
    "adopted_on",
    "source_url",
    "file_id",
    "status",
    "sort_order",
)


def _snapshot(doc: LegalDocument) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for field in _AUDITED_FIELDS:
        value = getattr(doc, field)
        if isinstance(value, datetime | date):
            data[field] = value.isoformat()
        elif isinstance(value, uuid.UUID):
            data[field] = str(value)
        else:
            data[field] = value
    return data


async def _document_or_404(db: AsyncSession, doc_id: uuid.UUID) -> LegalDocument:
    doc = await repo.get_legal_document(db, doc_id)
    if doc is None:
        raise err("ERR-SYS-003", details={"legal_document": str(doc_id)})
    return doc


async def _validate_file_id(db: AsyncSession, file_id: uuid.UUID | None) -> None:
    """An archived file cannot be (re)attached — `count_active_media_files` is
    the same check announcements runs over its whole set."""
    if file_id is None:
        return
    if await repo.count_active_media_files(db, [file_id]) != 1:
        raise err("ERR-VAL-001", details={"reason": "file_not_found"})


async def _file_ref(db: AsyncSession, doc: LegalDocument) -> FileRef | None:
    if doc.file_id is None:
        return None
    file = await db.get(MediaFile, doc.file_id)
    if file is None or file.status != "active":
        # An archived file leaves the row itself intact: the document still has
        # its number, its date and (usually) its lex.uz link.
        return None
    return FileRef(id=file.id, filename=file.filename, content_type=file.content_type)


async def _to_public_out(db: AsyncSession, doc: LegalDocument) -> LegalDocumentOut:
    return LegalDocumentOut(
        id=doc.id,
        title=doc.title,
        summary=doc.summary,
        doc_number=doc.doc_number,
        adopted_on=doc.adopted_on,
        source_url=doc.source_url,
        file=await _file_ref(db, doc),
    )


async def _to_admin_out(db: AsyncSession, doc: LegalDocument) -> LegalDocumentAdminOut:
    return LegalDocumentAdminOut(
        id=doc.id,
        title=doc.title,
        summary=doc.summary,
        doc_number=doc.doc_number,
        adopted_on=doc.adopted_on,
        source_url=doc.source_url,
        file=await _file_ref(db, doc),
        status=doc.status,
        sort_order=doc.sort_order,
        created_by=doc.created_by,
        created_at=doc.created_at,
    )


# --- The anonymous surface ------------------------------------------------------------


async def list_public(db: AsyncSession, *, params: PageParams) -> Page[LegalDocumentOut]:
    rows, total = await repo.list_public_legal_documents(
        db, offset=params.offset, limit=params.page_size
    )
    items = [await _to_public_out(db, row) for row in rows]
    return Page[LegalDocumentOut](
        items=items, total=total, page=params.page, page_size=params.page_size
    )


async def get_public(db: AsyncSession, *, doc_id: uuid.UUID) -> LegalDocumentOut:
    doc = await repo.get_public_legal_document(db, doc_id)
    if doc is None:
        raise err("ERR-SYS-003", details={"legal_document": str(doc_id)})
    return await _to_public_out(db, doc)


async def get_public_file(db: AsyncSession, *, doc_id: uuid.UUID) -> tuple[MediaFile, bytes]:
    """The anonymous download. Deliberately not `files.get_readable`: that one
    asks what an ACTOR may read, and there is no actor here. A 404 — never a
    403 — answers every miss, including a document that exists but is still a
    draft: to the internet the two cases must be indistinguishable."""
    doc = await repo.get_public_legal_document(db, doc_id)
    if doc is None or doc.file_id is None:
        raise err("ERR-SYS-003", details={"legal_document": str(doc_id)})
    file = await db.get(MediaFile, doc.file_id)
    if file is None or file.status != "active":
        raise err("ERR-SYS-003", details={"file": str(doc.file_id)})
    return file, await storage.get_object(file.storage_key)


# --- Admin CRUD -----------------------------------------------------------------------


async def list_admin(
    db: AsyncSession, *, params: PageParams, status: str | None
) -> Page[LegalDocumentAdminOut]:
    rows, total = await repo.list_admin_legal_documents(
        db, status=status, offset=params.offset, limit=params.page_size
    )
    items = [await _to_admin_out(db, row) for row in rows]
    return Page[LegalDocumentAdminOut](
        items=items, total=total, page=params.page, page_size=params.page_size
    )


async def get_admin(db: AsyncSession, *, doc_id: uuid.UUID) -> LegalDocumentAdminOut:
    return await _to_admin_out(db, await _document_or_404(db, doc_id))


async def create(
    db: AsyncSession, *, data: LegalDocumentCreateIn, actor: User
) -> LegalDocumentAdminOut:
    await _validate_file_id(db, data.file_id)
    doc = LegalDocument(
        title=data.title.root,
        summary=data.summary.root if data.summary is not None else None,
        doc_number=data.doc_number,
        adopted_on=data.adopted_on,
        source_url=data.source_url,
        file_id=data.file_id,
        sort_order=data.sort_order,
        created_by=actor.id,
    )
    await repo.add(db, doc)
    await audit.log(
        db,
        action="legal_document.create",
        user_id=actor.id,
        object_type="legal_document",
        object_id=doc.id,
        new_value=_snapshot(doc),
    )
    return await _to_admin_out(db, doc)


async def patch(
    db: AsyncSession, *, doc_id: uuid.UUID, data: LegalDocumentPatchIn, actor: User
) -> LegalDocumentAdminOut:
    doc = await _document_or_404(db, doc_id)
    if doc.status == "archived":
        raise err("ERR-VAL-001", details={"reason": "archived"})
    fields = data.model_dump(exclude_unset=True)
    before = _snapshot(doc)

    if "file_id" in fields:
        await _validate_file_id(db, fields["file_id"])
    if "title" in fields and data.title is not None:
        doc.title = data.title.root
    if "summary" in fields:
        doc.summary = data.summary.root if data.summary is not None else None
    for field in ("doc_number", "adopted_on", "source_url", "file_id", "sort_order"):
        if field in fields and fields[field] is not None:
            setattr(doc, field, fields[field])
    # `source_url` and `file_id` are the two a patch may legitimately CLEAR: a
    # link that turned out to be wrong, a file replaced by a link. The loop
    # above skips nulls (they mean "not sent" for every other field), so the
    # clearing is spelled out here.
    if fields.get("source_url", "") is None:
        doc.source_url = None
    if fields.get("file_id", "") is None:
        doc.file_id = None

    _reject_empty_published(doc)
    await db.flush()
    await audit.log(
        db,
        action="legal_document.update",
        user_id=actor.id,
        object_type="legal_document",
        object_id=doc.id,
        old_value=before,
        new_value=_snapshot(doc),
    )
    return await _to_admin_out(db, doc)


def _reject_empty_published(doc: LegalDocument) -> None:
    """A published row must keep something to open — checked on publish AND on
    every patch, because clearing the link of a live document would otherwise
    put the dead button back on the public page (ruling R3)."""
    if doc.status != "published":
        return
    if doc.file_id is None and not (doc.source_url or "").strip():
        raise err("ERR-VAL-001", details={"reason": "nothing_to_open"})


async def publish(db: AsyncSession, *, doc_id: uuid.UUID, actor: User) -> LegalDocumentAdminOut:
    doc = await _document_or_404(db, doc_id)
    if doc.status == "archived":
        raise err("ERR-VAL-001", details={"reason": "archived"})
    if doc.status == "published":
        raise err("ERR-VAL-001", details={"reason": "already_published"})
    if doc.file_id is None and not (doc.source_url or "").strip():
        raise err("ERR-VAL-001", details={"reason": "nothing_to_open"})
    before = _snapshot(doc)
    doc.status = "published"
    await db.flush()
    await audit.log(
        db,
        action="legal_document.publish",
        user_id=actor.id,
        object_type="legal_document",
        object_id=doc.id,
        old_value=before,
        new_value=_snapshot(doc),
    )
    return await _to_admin_out(db, doc)


async def archive(db: AsyncSession, *, doc_id: uuid.UUID, actor: User) -> LegalDocumentAdminOut:
    doc = await _document_or_404(db, doc_id)
    if doc.status != "archived":
        before = _snapshot(doc)
        doc.status = "archived"
        await db.flush()
        await audit.log(
            db,
            action="legal_document.archive",
            user_id=actor.id,
            object_type="legal_document",
            object_id=doc.id,
            old_value=before,
            new_value=_snapshot(doc),
        )
    return await _to_admin_out(db, doc)
