"""The anonymous announcements surface (`0037`) — what the public `landing` site
shows under "Yangiliklar va eʼlonlar". No `get_current_user`, no permission code,
a rate limit instead of both: the same idiom `norms.public_router`,
`permits.public_router` and `public.router` already established.

Kept in its own file, beside `announcements_router.py` rather than inside it, so
the whole anonymous surface of this module is one import away from a reader
asking "what can the internet reach" — the authenticated reader in that file
resolves a role and a region, and nothing here does.

A row reaches these routes only because an editor ticked `public_on_landing` on
an announcement with no audience; the service refuses the other combination.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files
from app.core.deps import get_db
from app.core.ratelimit import rate_limit
from app.core.schemas import Page, PageParams
from app.modules.admin import announcements_service as service
from app.modules.admin.announcements_service import AnnouncementLandingOut

router = APIRouter(prefix="/public/announcements", tags=["public"])

# One bucket for all three: the list and the item are small, cached-shaped reads
# over a table that changes a few times a month, and the download is capped by
# what an editor chose to attach — not an arbitrary-cost endpoint like the price
# estimate, which is why that one has a tighter limit of its own.
_LIMIT = Depends(rate_limit("public_announcements", "ratelimit_public_announcements_per_minute"))


@router.get("", response_model=Page[AnnouncementLandingOut], dependencies=[_LIMIT])
async def list_landing_announcements(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[PageParams, Depends()],
) -> Any:
    return await service.list_landing(db, params=params)


@router.get("/{announcement_id}", response_model=AnnouncementLandingOut, dependencies=[_LIMIT])
async def get_landing_announcement(
    announcement_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Any:
    return await service.get_landing(db, announcement_id=announcement_id)


@router.get("/{announcement_id}/files/{file_id}", dependencies=[_LIMIT])
async def download_landing_file(
    announcement_id: uuid.UUID,
    file_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The attachment is addressed THROUGH its announcement — `GET /files/{id}`
    needs a session, so without this route a public announcement's own PDF would
    be a link the visitor cannot open."""
    file, data = await service.get_landing_file(
        db, announcement_id=announcement_id, file_id=file_id
    )
    disposition = "inline" if file.content_type in files.INLINE_TYPES else "attachment"
    return Response(
        content=data,
        media_type=file.content_type,
        headers={
            "Content-Disposition": files.content_disposition(disposition, file.filename),
            "X-Content-Type-Options": "nosniff",
        },
    )
