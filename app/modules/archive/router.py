"""`archive` — a level-5 reader with one write action (design/01 rule 5). Reads
(the register, one item) require `archive.view`; archiving and verifying
require `archive.manage` (F23, `docs/plans/07.3-findings.md`: the two used to
share one code, which meant a read-only role could never get the read without
also getting the write). The zone restriction lives in `service.py`."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import xlsx
from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.core.time import business_today
from app.modules.archive import export, service
from app.modules.archive.permissions import ARCHIVE_MANAGE, ARCHIVE_VIEW
from app.modules.archive.schemas import (
    ArchivableObjectType,
    ArchiveItemOut,
    ArchiveItemStatus,
    ArchiveRequestIn,
)
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User

router = APIRouter(tags=["archive"])

# Route order matters here: `POST /archive/{item_id}/verify` and
# `POST /archive/{object_type}/{object_id}` are the same two-segment shape,
# and Starlette matches routes in REGISTRATION order using the path pattern
# alone — a `{object_type}` segment matches the literal text "verify" just as
# well as "application", so if the generic route were registered first every
# `POST /archive/<uuid>/verify` would be captured by IT instead, and 422 on
# `object_type` not being a recognised `ArchivableObjectType` (found by this
# module's own test suite, not by inspection). The literal-suffixed route
# goes first, precisely because Starlette does not.


@router.get("/archive", response_model=Page[ArchiveItemOut])
async def list_archive_items(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ARCHIVE_VIEW))],
    params: Annotated[PageParams, Depends()],
    object_type: ArchivableObjectType | None = None,
    filter_status: Annotated[ArchiveItemStatus | None, Query(alias="status")] = None,
) -> Page[ArchiveItemOut]:
    return await service.list_items(
        db, actor=actor, params=params, object_type=object_type, status=filter_status
    )


@router.get("/archive/export.xlsx")
async def export_archive_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ARCHIVE_VIEW))],
    lang: xlsx.Lang = "uz_latn",
    object_type: ArchivableObjectType | None = None,
    filter_status: Annotated[ArchiveItemStatus | None, Query(alias="status")] = None,
) -> Response:
    """`GET /archive` as a spreadsheet (stage 13, ruling #204): the same
    filters, the same zone, every matching row up to the configured cap.
    Declared before `/archive/{item_id}` on purpose — a UUID path parser
    would otherwise answer this literal path with a worse error than a 404."""
    items, total, cap = await export.rows(
        db, actor=actor, lang=lang, object_type=object_type, status=filter_status
    )
    filename = f"arxiv-reyestri-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.post("/archive/{item_id}/verify", response_model=ArchiveItemOut)
async def verify_archive_item(
    item_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ARCHIVE_MANAGE))],
) -> ArchiveItemOut:
    item = await service.verify_item(db, actor, item_id)
    return ArchiveItemOut.model_validate(item)


@router.get("/archive/{item_id}", response_model=ArchiveItemOut)
async def get_archive_item(
    item_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ARCHIVE_VIEW))],
) -> ArchiveItemOut:
    item = await service.get_item(db, actor, item_id)
    return ArchiveItemOut.model_validate(item)


@router.post("/archive/{object_type}/{object_id}", response_model=ArchiveItemOut)
async def archive_object(
    object_type: ArchivableObjectType,
    object_id: uuid.UUID,
    data: ArchiveRequestIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(ARCHIVE_MANAGE))],
) -> ArchiveItemOut:
    item = await service.archive_object(
        db,
        actor=actor,
        object_type=object_type,
        object_id=object_id,
        retention_until=data.retention_until,
    )
    return ArchiveItemOut.model_validate(item)
