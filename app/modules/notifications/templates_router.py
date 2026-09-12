"""Admin CRUD over notification templates (С19: the administrator manages texts).
The router lives in `notifications` — the module owns the permission code, and a
level-2 module may depend on `auth` (level 1) for its gates."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import xlsx
from app.core.deps import get_db
from app.core.errors import err
from app.core.schemas import Page, PageParams
from app.core.time import business_today
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User
from app.modules.notifications import export, repo, service
from app.modules.notifications.models import NotificationTemplate
from app.modules.notifications.permissions import TEMPLATES_MANAGE
from app.modules.notifications.schemas import SMS_MODERATION_WARNING, TemplateIn, TemplateOut

router = APIRouter(prefix="/admin/notification-templates", tags=["admin"])


def _out(row: NotificationTemplate) -> TemplateOut:
    out = TemplateOut.model_validate(row, from_attributes=True)
    if row.channel == "sms":
        out.warning = SMS_MODERATION_WARNING
    return out


@router.get("", response_model=Page[TemplateOut])
async def list_templates(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[PageParams, Depends()],
    actor: Annotated[User, Depends(require_permission(TEMPLATES_MANAGE))],
    event_code: str | None = None,
    channel: str | None = None,
    status: str | None = None,
) -> Page[TemplateOut]:
    rows, total = await repo.list_templates(
        db,
        event_code=event_code,
        channel=channel,
        status=status,
        page=params.page,
        page_size=params.page_size,
    )
    return Page[TemplateOut](
        items=[_out(row) for row in rows], total=total, page=params.page, page_size=params.page_size
    )


@router.get("/export.xlsx")
async def export_templates_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(TEMPLATES_MANAGE))],
    lang: xlsx.Lang = "uz_latn",
    event_code: str | None = None,
    channel: str | None = None,
    status: str | None = None,
) -> Response:
    """`GET /admin/notification-templates` as a spreadsheet (stage 13,
    ruling #204): the same filters, the same permission gate, every
    matching row up to the configured cap. Declared before
    `/admin/notification-templates/{template_id}` on purpose."""
    items, total, cap = await export.rows(
        db, lang=lang, event_code=event_code, channel=channel, status=status
    )
    filename = f"xabar-shablonlari-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.post("", response_model=TemplateOut, status_code=201)
async def create_template(
    body: TemplateIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(TEMPLATES_MANAGE))],
) -> TemplateOut:
    row = await service.create_template(
        db, body, actor_id=actor.id, ip=request.client.host if request.client else None
    )
    return _out(row)


@router.get("/{template_id}", response_model=TemplateOut)
async def get_template(
    template_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(TEMPLATES_MANAGE))],
) -> TemplateOut:
    row = await repo.get_template(db, template_id)
    if row is None:
        raise err("ERR-SYS-003", details={"template": str(template_id)})
    return _out(row)


@router.post("/{template_id}", response_model=TemplateOut)
async def supersede_template(
    template_id: uuid.UUID,
    body: TemplateIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(TEMPLATES_MANAGE))],
) -> TemplateOut:
    row = await service.supersede_template(
        db, template_id, body, actor_id=actor.id, ip=request.client.host if request.client else None
    )
    return _out(row)


@router.post("/{template_id}/archive", response_model=TemplateOut)
async def archive_template(
    template_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(TEMPLATES_MANAGE))],
) -> TemplateOut:
    row = await service.archive_template(
        db, template_id, actor_id=actor.id, ip=request.client.host if request.client else None
    )
    return _out(row)
