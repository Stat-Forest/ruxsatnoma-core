"""FAQ CRUD — `help.faq.manage` on every route (`router.py`'s public `GET
/help/faq` is the read side, mirroring `admin.announcements_router`'s own
`router`/`admin_router` split)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import xlsx
from app.core.deps import get_db
from app.core.time import business_today
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User
from app.modules.help import export, service
from app.modules.help.permissions import FAQ_MANAGE
from app.modules.help.schemas import FaqIn, FaqOut, FaqPatch

router = APIRouter(prefix="/admin/help/faq", tags=["admin"])
_MANAGE = Depends(require_permission(FAQ_MANAGE))


@router.get("", response_model=list[FaqOut], dependencies=[_MANAGE])
async def list_faq(db: Annotated[AsyncSession, Depends(get_db)], status: str | None = None) -> Any:
    return await service.list_faq_admin(db, status=status)


@router.get("/export.xlsx", dependencies=[_MANAGE])
async def export_faq_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    lang: xlsx.Lang = "uz_latn",
    status: str | None = None,
) -> Response:
    """`GET /admin/help/faq` as a spreadsheet (stage 13, ruling #204): the
    same permission, the same filter, the whole (unpaged) list truncated to
    the cap in Python. Declared before `/{faq_id}` on purpose — that sibling
    is a PATCH, not a GET, but the convention holds regardless."""
    items, total, cap = await export.faq_rows(db, lang=lang, status=status)
    filename = f"faq-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render_faq(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.post("", response_model=FaqOut, status_code=201)
async def create_faq(
    payload: FaqIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(FAQ_MANAGE))],
) -> Any:
    return await service.create_faq(db, data=payload, actor=user)


@router.patch("/{faq_id}", response_model=FaqOut)
async def update_faq(
    faq_id: uuid.UUID,
    payload: FaqPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(FAQ_MANAGE))],
) -> Any:
    return await service.update_faq(db, faq_id, data=payload, actor=user)
