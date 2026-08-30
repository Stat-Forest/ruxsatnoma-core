"""Geodata import: the multipart upload and the batch's status.

Both routes are `CONTOURS_MANAGE` (the GIS specialist draws, edits AND imports,
but never approves — the batch's own approve/publish arrive in Task 8) and both
are ALSO zone-scoped in the service on `organization_id`, a separate gate from
the permission check (lesson: "Zone scoping is not a permission check").

`POST /gis/imports` additionally requires an `Idempotency-Key` — a retried
upload must replay its 202, never queue a second batch (see the route's own
docstring). `ERR-SYS-005` means an Idempotency-Key conflict here and nothing
else; this module's own state conflicts are `ERR-GIS-005`.

The upload is capped BEFORE the body exists as one `bytes` object (ruling 8,
lesson: "A cap checked after reading the body is not a cap"): `read_capped`
rejects an oversized `Content-Length` without reading a byte and otherwise
streams in chunks, aborting the instant the running total passes
`gis_import_max_mb`. `save_upload` re-checks as the final authority, against
gis's OWN MIME/magic table rather than the document whitelist `POST /files`
uses.
"""

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files, settings_store
from app.core.deps import get_db
from app.core.errors import err
from app.core.idempotency import IdempotencyContext
from app.modules.auth.deps import idempotency_context, require_permission
from app.modules.auth.models import User
from app.modules.gis import import_service
from app.modules.gis import service as gis_service
from app.modules.gis.permissions import CONTOURS_APPROVE, CONTOURS_MANAGE
from app.modules.gis.schemas import ImportAccepted, ImportOut, PublishImportOut

router = APIRouter(prefix="/gis", tags=["gis"])


def _parse_attributes(raw: str) -> dict[str, Any]:
    """`attributes` arrives as a JSON STRING inside the multipart body — a form
    field cannot carry a nested object — so it is decoded here, at the transport
    edge, and validated to be the flat `{our key: the file's field name}` map
    `import_service._map_features` expects (ruling 13: mapping is explicit and
    per-import, because the Agency's field names are truncated Excel-join
    artefacts like `c152_Exc_2` and differ per file).

    Anything else is `ERR-VAL-001` rather than a 500 later in the job: this
    value is stored in a JSONB column and then read as field names, so a list,
    a nested object or a number would fail somewhere far from the request that
    supplied it.
    """
    try:
        parsed = json.loads(raw or "{}")
    # Deliberately parenthesized, not the PEP 758 bare form (see
    # core.settings_store.coerce for the reasoning).
    except (ValueError, TypeError):  # fmt: skip
        raise err("ERR-VAL-001", details={"reason": "attributes_not_json"}) from None
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise err("ERR-VAL-001", details={"reason": "attributes_not_a_string_map"})
    return parsed


@router.post("/imports", status_code=202)
async def create_import(
    request: Request,
    file: UploadFile,
    layer_code: Annotated[str, Form()],
    organization_id: Annotated[uuid.UUID, Form()],
    approval_doc_id: Annotated[uuid.UUID, Form()],
    fmt: Annotated[str, Form(alias="format")],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
    ctx: Annotated[IdempotencyContext, Depends(idempotency_context)],
    attributes: Annotated[str, Form()] = "{}",
) -> ImportAccepted:
    """202, not 201: the file is stored and the work is QUEUED (ruling 6). The
    parse happens in `jobs.process_gis_imports`, which is why a 151-feature
    delivery does not need the browser to stay open — the initiator gets a
    `gis.import.finished` notification when it lands.

    Idempotency-Key is MANDATORY (3.4's mechanism, `app/core/idempotency.py`;
    this is its first consumer). Without it a retried or double-clicked upload
    files a SECOND batch which then succeeds — 151 `duplicate_number` warnings
    and a `/2` suffix on every contour — and there is no delete path: archiving
    is one contour at a time. `ctx.save()` runs before the response so a replay
    of the same key returns the stored 202 with the ORIGINAL `import_id`
    instead of queueing a duplicate. `ctx` is declared after `user` so the
    permission check runs first and an unauthorized caller never mints a
    marker row.
    """
    cap_bytes = await settings_store.get_int(db, "gis_import_max_mb") * 1024 * 1024
    data = await files.read_capped(file, cap_bytes, files.declared_length(request.headers))
    row = await import_service.create_import(
        db,
        layer_code=layer_code,
        organization_id=organization_id,
        approval_doc_id=approval_doc_id,
        fmt=fmt,
        attribute_map=_parse_attributes(attributes),
        data=data,
        filename=files.sanitize_filename(file.filename or "import"),
        content_type=file.content_type or "application/octet-stream",
        actor=user,
    )
    accepted = ImportAccepted(import_id=row.id)
    await ctx.save(db, status_code=202, body=accepted.model_dump(mode="json"))
    return accepted


@router.get("/imports/{import_id}")
async def get_import(
    import_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
) -> ImportOut:
    row = await import_service.get_import(db, import_id, actor=user)
    return ImportOut.model_validate(row, from_attributes=True)


@router.post("/imports/{import_id}/submit-review")
async def submit_import_review(
    import_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
) -> ImportOut:
    """The GIS specialist who ran the import submits their own batch —
    `CONTOURS_MANAGE`, the same permission `POST /gis/imports` itself uses."""
    row = await gis_service.submit_import_review(db, import_id, actor=user)
    return ImportOut.model_validate(row, from_attributes=True)


@router.post("/imports/{import_id}/approve")
async def approve_import(
    import_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_APPROVE))],
) -> ImportOut:
    """The rahbar approves the whole batch at once (ruling 3) — `CONTOURS_APPROVE`,
    never the specialist who filed it."""
    row = await gis_service.approve_import(db, import_id, actor=user)
    return ImportOut.model_validate(row, from_attributes=True)


@router.post("/imports/{import_id}/publish")
async def publish_import(
    import_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_APPROVE))],
) -> PublishImportOut:
    result = await gis_service.publish_import(db, import_id, actor=user)
    return PublishImportOut.model_validate(result)
