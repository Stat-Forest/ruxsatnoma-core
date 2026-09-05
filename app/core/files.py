"""File subsystem service (rulings 3–5, 3.3b): validation, persistence, access.

Core cannot import module models, so module-specific read grants plug in through
ACCESS_CHECKS — the same registration idiom as the permission registry. admin
appends a checker that opens files attached to announcements the caller can see.
"""

import hashlib
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from fastapi import UploadFile
from geoalchemy2.elements import WKTElement
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, storage
from app.core.errors import err
from app.core.models import MediaFile
from app.db import uuid7

# content_type -> accepted magic prefixes (any match passes)
ALLOWED_TYPES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/webp": (b"RIFF",),  # + b"WEBP" at offset 8, checked below
}

INLINE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})

AccessChecker = Callable[[AsyncSession, uuid.UUID, Any], Awaitable[bool]]
ACCESS_CHECKS: list[AccessChecker] = []

# Streamed in fixed chunks so a body over the cap never materializes fully in RAM
# (I2, 3.3b final review) — small enough to keep the abort latency low, large
# enough that the chunk-count overhead is negligible next to real uploads.
_UPLOAD_CHUNK_SIZE = 1024 * 1024


_NON_ASCII_PRINTABLE = re.compile(r"[^\x20-\x7e]")


def _ascii_fallback_filename(filename: str) -> str:
    """RFC 6266 fallback `filename=` value for user agents that ignore the extended
    `filename*` parameter. Starlette encodes header values as latin-1, so a raw
    Cyrillic (or any non-latin-1) byte here would raise UnicodeEncodeError on every
    download (C1, 3.3b final review). The base name and extension are ASCII-filtered
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


def sanitize_filename(filename: str) -> str:
    """Strip characters that would break the Content-Disposition header (quotes end
    the filename="..." value early, carriage returns/newlines inject headers) —
    applied once at every ingest point so the stored value is already safe
    wherever it is later reflected back — and again inside `content_disposition`
    below, which must not depend on a caller having remembered."""
    return filename.replace('"', "").replace("\r", "").replace("\n", " ")


def content_disposition(disposition: str, filename: str) -> str:
    """RFC 6266 / RFC 5987: the legacy ASCII-only `filename=` alongside
    `filename*=UTF-8''<percent-encoded>`, which carries the exact original name for
    clients that understand it. Fixes C1 (3.3b final review): a Cyrillic filename
    («доверенность.pdf») previously 500'd every download.

    In `core` rather than in `app/files_router.py` where it was written, because
    it is now shared: `GET /permits/{id}/pdf` (3.11a t8) serves a name built from
    `permits.series`, which is the CYRILLIC «А» — the exact byte that raises — and
    the lesson's own instruction is that every new download endpoint reuses one
    helper rather than growing a second, subtly different copy.

    **`sanitize_filename` runs HERE rather than being a precondition on callers.**
    A `"` is printable ASCII, so `_ascii_fallback_filename` keeps it and it closes
    the `filename="…"` value early: `'a".pdf'` emitted `filename="a"b.pdf"`. Today
    every name reaching this function was sanitized at its ingest point, so it is
    malformation and not header injection (CR/LF are stripped by the same call) —
    but a precondition stated in a docstring is enforced by nobody, and stage 4
    adds callers to a helper that is now shared by three routers. Sanitizing is
    idempotent, so a caller that already did it loses nothing, and `filename*`
    still carries the exact stored name: `quote(..., safe="")` percent-encodes
    every one of these characters anyway."""
    filename = sanitize_filename(filename)
    ascii_name = _ascii_fallback_filename(filename)
    encoded = quote(filename, safe="")
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def declared_length(headers: Mapping[str, str]) -> int | None:
    """The request's own `Content-Length` as an int, or None when it is absent or
    malformed — in which case `read_capped` falls through to the chunked read and
    enforces the cap there. Shared by every capped ingest route (`POST /files`,
    `POST /gis/imports`) so the parse of an attacker-controlled header has one
    definition, not one per router."""
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    # Deliberately parenthesized, not the PEP 758 bare form (see
    # settings_store.coerce for the reasoning).
    except (TypeError, ValueError):  # fmt: skip
        return None


async def read_capped(file: UploadFile, cap_bytes: int, content_length: int | None) -> bytes:
    """Enforces the upload size cap before the body sits fully in RAM as one
    `bytes` object (I2, 3.3b final review — the plan's ruling 3 says the cap is
    enforced first, but it was only checked after `file.read()` had already
    pulled everything into memory). A `Content-Length` or Starlette's own
    spooled-upload `file.size`, when known, rejects oversized bodies without
    reading a single byte; otherwise the body streams in fixed chunks and aborts
    the instant the running total exceeds the cap. `save_upload`'s own check
    stays as the final authority (defense in depth) once this returns."""
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


def _magic_ok(content_type: str, data: bytes, allowed: Mapping[str, tuple[bytes, ...]]) -> bool:
    """An EMPTY prefix tuple means "this type has no reliable magic" and passes —
    the only such entry today is gis's `text/csv` (a CSV starts with whatever its
    first column header happens to be), where the real gate is the parser itself.
    `any()` over an empty tuple is False, so without this branch such a type
    could never be uploaded at all."""
    prefixes = allowed[content_type]
    if prefixes and not any(data.startswith(p) for p in prefixes):
        return False
    if content_type == "image/webp":
        return data[8:12] == b"WEBP"
    return True


async def save_upload(
    db: AsyncSession,
    *,
    data: bytes,
    filename: str,
    content_type: str,
    actor: Any,
    allowed: Mapping[str, tuple[bytes, ...]] | None = None,
    cap_key: str = "max_upload_mb",
) -> MediaFile:
    """Persist one upload. `allowed`/`cap_key` let a caller with its own ingest
    policy (gis's geodata import: a different MIME/magic table and the larger
    `gis_import_max_mb` cap) reuse this path instead of widening the document
    whitelist for every uploader in the system (plan 03.6a ruling 8)."""
    table = ALLOWED_TYPES if allowed is None else allowed
    if content_type not in table:
        raise err("ERR-VAL-001", details={"reason": "type_not_allowed"})
    cap = await settings_store.get_int(db, cap_key) * 1024 * 1024
    if len(data) > cap:
        raise err("ERR-VAL-001", details={"reason": "too_large"})
    if not _magic_ok(content_type, data, table):
        raise err("ERR-VAL-001", details={"reason": "content_mismatch"})
    file_id = uuid7()
    file = MediaFile(
        id=file_id,
        storage_key=f"{datetime.now(UTC):%Y/%m}/{file_id}",
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        uploaded_by=actor.id,
    )
    # MinIO write happens BEFORE the DB flush: a storage failure then aborts the
    # transaction and leaves no dangling row; a dangling MinIO object left behind
    # by a later DB failure is harmless garbage.
    await storage.put_object(file.storage_key, data, content_type)
    db.add(file)
    await db.flush()
    return file


async def set_capture_metadata(
    db: AsyncSession,
    file_id: uuid.UUID,
    *,
    taken_at: datetime | None = None,
    gps: tuple[float, float] | None = None,
    device: dict[str, Any] | None = None,
) -> MediaFile:
    """Fill `taken_at`/`gps`/`device` on an ALREADY-UPLOADED `media_files` row
    — those three columns exist since 3.3b ("inspector photo metadata",
    design/02) with no writer until stage 4.1. Deliberately a second call
    rather than new parameters on `save_upload`: the generic `POST /files`
    route stays untouched (design/01's own file-router exception), and a
    module attaching capture metadata does so through its OWN attach step,
    the same way any module attaches an already-uploaded file to its own
    object (`application_documents`, `inspection_act_files`) rather than a
    second upload endpoint.

    `gps` is `(lon, lat)` — floats a caller's own pydantic schema already
    bounded, never raw text reaching the WKT literal this builds. Raises
    `ERR-SYS-003` for an unknown or archived file, the same code
    `get_readable` uses for the same situation."""
    file = await db.get(MediaFile, file_id)
    if file is None or file.status != "active":
        raise err("ERR-SYS-003")
    if taken_at is not None:
        file.taken_at = taken_at
    if gps is not None:
        file.gps = WKTElement(f"POINT({gps[0]} {gps[1]})", srid=4326)
    if device is not None:
        file.device = device
    await db.flush()
    return file


async def get_readable(
    db: AsyncSession, file_id: uuid.UUID, actor: Any, role_code: str | None
) -> tuple[MediaFile, bytes]:
    file = await db.get(MediaFile, file_id)
    if file is None or file.status != "active":
        raise err("ERR-SYS-003")
    allowed = file.uploaded_by == actor.id or (role_code is not None and role_code != "applicant")
    if not allowed:
        for check in ACCESS_CHECKS:
            if await check(db, file_id, actor):
                allowed = True
                break
    if not allowed:
        raise err("ERR-ACL-001")
    data = await storage.get_object(file.storage_key)
    return file, data
