"""The anonymous legal-documents surface (`0043`) — what the public `landing`
site shows under "Normativ-huquqiy hujjatlar". No `get_current_user`, no
permission code, a rate limit instead of both: the idiom
`announcements_public_router`, `norms.public_router` and `public.router`
already established.

Kept in its own file, beside `legal_documents_router.py` rather than inside it,
so the whole anonymous surface of this module is one import away from a reader
asking "what can the internet reach".

A row reaches these routes only by being published, and `legal_documents_
service.publish` refuses to publish one that has neither a file nor a link — so
nothing here can hand a visitor a document with nothing to open.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files
from app.core.deps import get_db
from app.core.ratelimit import rate_limit
from app.core.schemas import Page, PageParams
from app.modules.admin import legal_documents_service as service
from app.modules.admin.legal_documents_service import LegalDocumentOut

router = APIRouter(prefix="/public/legal-documents", tags=["public"])

# One bucket for all three, the same reasoning `public_announcements` carries:
# the list and the item are small, cached-shaped reads over a table an editor
# appends to a few times a year, and the download is capped by what that editor
# chose to attach.
_LIMIT = Depends(
    rate_limit("public_legal_documents", "ratelimit_public_legal_documents_per_minute")
)


@router.get("", response_model=Page[LegalDocumentOut], dependencies=[_LIMIT])
async def list_public_legal_documents(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[PageParams, Depends()],
) -> Any:
    return await service.list_public(db, params=params)


@router.get("/{doc_id}", response_model=LegalDocumentOut, dependencies=[_LIMIT])
async def get_public_legal_document(
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Any:
    return await service.get_public(db, doc_id=doc_id)


@router.get("/{doc_id}/file", dependencies=[_LIMIT])
async def download_public_legal_document(
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The document is addressed by ITS id, not the file's: `GET /files/{id}`
    needs a session, so without this route a public document's own PDF would be
    a link the visitor cannot open."""
    file, data = await service.get_public_file(db, doc_id=doc_id)
    disposition = "inline" if file.content_type in files.INLINE_TYPES else "attachment"
    return Response(
        content=data,
        media_type=file.content_type,
        headers={
            "Content-Disposition": files.content_disposition(disposition, file.filename),
            "X-Content-Type-Options": "nosniff",
        },
    )
