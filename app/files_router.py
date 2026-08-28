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
from app.core.errors import err
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth.deps import get_current_user
from app.modules.auth.models import User

router = APIRouter(prefix="/files", tags=["files"])

# Streamed in fixed chunks so a body over the cap never materializes fully in RAM
# (I2, final review) — small enough to keep the abort latency low, large enough
# that the chunk-count overhead is negligible next to real uploads.
_UPLOAD_CHUNK_SIZE = 1024 * 1024


class FileOut(BaseModel):
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


def _sanitize_filename(filename: str) -> str:
    """Strip characters that would break the Content-Disposition header (quotes end
    the filename="..." value early, carriage returns/newlines inject headers) —
    applied once here so the stored value is already safe wherever it is later
    reflected back."""
    return filename.replace('"', "").replace("\r", "").replace("\n", " ")


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


async def _read_capped(file: UploadFile, cap_bytes: int, content_length: int | None) -> bytes:
    """Enforces the upload size cap before the body sits fully in RAM as one
    `bytes` object (I2, final review — the plan's ruling 3 says the cap is
    enforced first, but it was only checked after `file.read()` had already
    pulled everything into memory). A `Content-Length` or Starlette's own
    spooled-upload `file.size`, when known, rejects oversized bodies without
    reading a single byte; otherwise the body streams in fixed chunks and aborts
    the instant the running total exceeds the cap. `files.save_upload`'s own
    check stays as the final authority (defense in depth) once this returns."""
    if content_length is not None and content_length > cap_bytes:
        raise err("ERR-VAL-001", details={"reason": "too_large"})
    if file.size is not None and file.size > cap_bytes:
        raise err("ERR-VAL-001", details={"reason": "too_large"})

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > cap_bytes:
            raise err("ERR-VAL-001", details={"reason": "too_large"})
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("", status_code=201)
async def upload_file(
    request: Request,
    file: UploadFile,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> FileOut:
    cap_bytes = await settings_store.get_int(db, "max_upload_mb") * 1024 * 1024
    raw_content_length = request.headers.get("content-length")
    content_length: int | None = None
    if raw_content_length is not None:
        try:
            content_length = int(raw_content_length)
        except ValueError:
            content_length = None  # malformed header — fall through to the chunked read
    data = await _read_capped(file, cap_bytes, content_length)
    filename = _sanitize_filename(file.filename or "file")
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
