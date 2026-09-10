"""Legal-documents CRUD (`0043`), gated behind `admin.legal_documents.manage`.

The anonymous half lives in `legal_documents_public_router.py`, not here — the
same split `announcements_router` / `announcements_public_router` uses, so the
whole internet-reachable surface of this module stays one import away from a
reader asking what a visitor can get to.

There is no authenticated "reader" route between the two: unlike an
announcement, a legal act is not addressed at a role or a region, and staff read
the same public register a citizen does."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import xlsx
from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.core.time import business_today
from app.modules.admin import export
from app.modules.admin import legal_documents_service as service
from app.modules.admin.legal_documents_service import (
    LegalDocumentAdminOut,
    LegalDocumentCreateIn,
    LegalDocumentPatchIn,
)
from app.modules.admin.permissions import LEGAL_DOCUMENTS_MANAGE
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User

router = APIRouter(prefix="/admin/legal-documents", tags=["admin"])


@router.get("", response_model=Page[LegalDocumentAdminOut])
async def list_legal_documents(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[PageParams, Depends()],
    actor: Annotated[User, Depends(require_permission(LEGAL_DOCUMENTS_MANAGE))],
    status: str | None = None,
) -> Page[LegalDocumentAdminOut]:
    return await service.list_admin(db, params=params, status=status)


@router.get("/export.xlsx")
async def export_legal_documents_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(LEGAL_DOCUMENTS_MANAGE))],
    lang: xlsx.Lang = "uz_latn",
    status: str | None = None,
) -> Response:
    """`GET /admin/legal-documents` as a spreadsheet (stage 13, ruling
    #204): the same filter, the same permission gate, every matching row up
    to the configured cap. Declared before `/admin/legal-documents/{doc_id}`
    on purpose."""
    items, total, cap = await export.legal_documents_rows(db, lang=lang, status=status)
    filename = f"meyoriy-hujjatlar-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render_legal_documents(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.get("/{doc_id}", response_model=LegalDocumentAdminOut)
async def get_legal_document(
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(LEGAL_DOCUMENTS_MANAGE))],
) -> LegalDocumentAdminOut:
    return await service.get_admin(db, doc_id=doc_id)


@router.post("", response_model=LegalDocumentAdminOut, status_code=201)
async def create_legal_document(
    body: LegalDocumentCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(LEGAL_DOCUMENTS_MANAGE))],
) -> LegalDocumentAdminOut:
    return await service.create(db, data=body, actor=actor)


@router.patch("/{doc_id}", response_model=LegalDocumentAdminOut)
async def patch_legal_document(
    doc_id: uuid.UUID,
    body: LegalDocumentPatchIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(LEGAL_DOCUMENTS_MANAGE))],
) -> LegalDocumentAdminOut:
    return await service.patch(db, doc_id=doc_id, data=body, actor=actor)


@router.post("/{doc_id}/publish", response_model=LegalDocumentAdminOut)
async def publish_legal_document(
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(LEGAL_DOCUMENTS_MANAGE))],
) -> LegalDocumentAdminOut:
    return await service.publish(db, doc_id=doc_id, actor=actor)


@router.post("/{doc_id}/archive", response_model=LegalDocumentAdminOut)
async def archive_legal_document(
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(LEGAL_DOCUMENTS_MANAGE))],
) -> LegalDocumentAdminOut:
    return await service.archive(db, doc_id=doc_id, actor=actor)
