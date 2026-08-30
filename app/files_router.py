"""Infrastructure file router (design/01's file-router exception; lives at the app
level, not in core, so core keeps importing zero modules — auth deps and audit are
module code)."""

import re
import uuid
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files, settings_store
from app.core.deps import get_db
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth.deps import get_current_user
from app.modules.auth.models import User

router = APIRouter(prefix="/files", tags=["files"])


class FileOut(BaseModel):
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


_NON_ASCII_PRINTABLE = re.compile(r"[^\x20-\x7e]")


def _ascii_fallback_filename(filename: str) -> str:
    """RFC 6266 fallback `filename=` value for user agents that ignore the extended
    `filename*` parameter. Starlette encodes header values as latin-1, so a raw
    Cyrillic (or any non-latin-1) byte here would raise UnicodeEncodeError on every
    download (C1, final review). The base name and extension are ASCII-filtered
    independently; when the base name had real content that the filter wiped out
    entirely (e.g. a purely Cyrillic name), it is replaced with "file" — but a
    surviving extension is still appended, and a name with nothing left at all
    (no extension either) collapses to bare "file"."""
    stem, dot, ext = filename.rpartition(".")
    if not dot:  # no "." anywhere: rpartition puts the whole string in `ext`
        stem, ext = ext, ""
    ascii_stem = _NON_ASCII_PRINTABLE.sub("", stem).strip()
    ascii_ext = _NON_ASCII_PRINTABLE.sub("", ext).strip()
    base = "file" if stem and not ascii_stem else ascii_stem
    return (f"{base}.{ascii_ext}" if ascii_ext else base) or "file"


def _content_disposition(disposition: str, filename: str) -> str:
    """RFC 6266 / RFC 5987: the legacy ASCII-only `filename=` alongside
    `filename*=UTF-8''<percent-encoded>`, which carries the exact original name for
    clients that understand it. Fixes C1 (final review): a Cyrillic filename
    («доверенность.pdf») previously 500'd every download."""
    ascii_name = _ascii_fallback_filename(filename)
    encoded = quote(filename, safe="")
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


@router.post("", status_code=201)
async def upload_file(
    request: Request,
    file: UploadFile,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> FileOut:
    cap_bytes = await settings_store.get_int(db, "max_upload_mb") * 1024 * 1024
    data = await files.read_capped(file, cap_bytes, files.declared_length(request.headers))
    filename = files.sanitize_filename(file.filename or "file")
    saved = await files.save_upload(
        db,
        data=data,
        filename=filename,
        content_type=file.content_type or "application/octet-stream",
        actor=user,
    )
    await audit.log(
        db,
        action="file.upload",
        user_id=user.id,
        object_type="media_file",
        object_id=saved.id,
        new_value={
            "filename": saved.filename,
            "content_type": saved.content_type,
            "size": saved.size_bytes,
        },
    )
    return FileOut.model_validate(saved, from_attributes=True)


@router.get("/{file_id}")
async def download_file(
    file_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> Response:
    role = await auth_repo.role_code(db, user)
    file, data = await files.get_readable(db, file_id, user, role)
    disposition = "inline" if file.content_type in files.INLINE_TYPES else "attachment"
    return Response(
        content=data,
        media_type=file.content_type,
        headers={
            "Content-Disposition": _content_disposition(disposition, file.filename),
            "X-Content-Type-Options": "nosniff",
        },
    )
