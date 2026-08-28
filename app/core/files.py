"""File subsystem service (rulings 3–5, 3.3b): validation, persistence, access.

Core cannot import module models, so module-specific read grants plug in through
ACCESS_CHECKS — the same registration idiom as the permission registry. admin
appends a checker that opens files attached to announcements the caller can see.
"""

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

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


def _magic_ok(content_type: str, data: bytes) -> bool:
    prefixes = ALLOWED_TYPES[content_type]
    if not any(data.startswith(p) for p in prefixes):
        return False
    if content_type == "image/webp":
        return data[8:12] == b"WEBP"
    return True


async def save_upload(
    db: AsyncSession, *, data: bytes, filename: str, content_type: str, actor: Any
) -> MediaFile:
    if content_type not in ALLOWED_TYPES:
        raise err("ERR-VAL-001", details={"reason": "type_not_allowed"})
    cap = await settings_store.get_int(db, "max_upload_mb") * 1024 * 1024
    if len(data) > cap:
        raise err("ERR-VAL-001", details={"reason": "too_large"})
    if not _magic_ok(content_type, data):
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
