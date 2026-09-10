"""`/payments/recipients` — the split's own directory (stage 7.9 task 3,
decision #157). Three routes, deliberately: `GET` (list), `POST` (create),
`PATCH` (edit or deactivate) — and NO `DELETE`, because a recipient is
deactivated, never deleted (ruling #157: a deleted row breaks every report
over a period in which it was paid).

Reading is wider than writing (Override 2 / ruling R5): `_READ` accepts
EITHER `payments.view` or `PAYMENTS_RECIPIENTS_MANAGE`, because an
accountant must be able to see the directory to make sense of how an
invoice divided, even though the accountant role holds no grant to edit it.
`_WRITE` accepts only `PAYMENTS_RECIPIENTS_MANAGE`, which no role holds yet
— `require_permission` lets `sys_admin` through before it ever checks a
code (decision #41 ruling 2), so today's only writer is the superuser, and
this router never tests `user.is_superuser` itself (that spelling is the
one direct, code-only superuser check in this codebase; widening who else
may write here is a `role_permissions` row, not a change here)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import xlsx
from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.core.time import business_today
from app.modules.auth.deps import require_any_permission, require_permission
from app.modules.auth.models import User
from app.modules.payments import export
from app.modules.payments import recipients_service as service
from app.modules.payments.permissions import PAYMENTS_RECIPIENTS_MANAGE, PAYMENTS_VIEW
from app.modules.payments.schemas import (
    PaymentRecipientIn,
    PaymentRecipientOut,
    PaymentRecipientPatch,
)

router = APIRouter(prefix="/payments/recipients", tags=["payments"])

_READ = require_any_permission(PAYMENTS_VIEW, PAYMENTS_RECIPIENTS_MANAGE)
_WRITE = require_permission(PAYMENTS_RECIPIENTS_MANAGE)


@router.get("", response_model=Page[PaymentRecipientOut])
async def list_recipients(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[PageParams, Depends()],
    actor: Annotated[User, Depends(_READ)],
) -> Page[PaymentRecipientOut]:
    return await service.list_all(db, params=params)


@router.get("/export.xlsx")
async def export_recipients_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(_READ)],
    lang: xlsx.Lang = "uz_latn",
) -> Response:
    """`GET /payments/recipients` as a spreadsheet (stage 13, ruling
    #204): the same read gate (`payments.view` or
    `payments.recipients.manage`), the whole directory (active and
    inactive alike, exactly like the list — ruling #157) up to the
    configured cap."""
    items, total, cap = await export.recipient_rows(db, lang=lang)
    filename = f"qabul-qiluvchilar-{business_today().isoformat()}.xlsx"
    rendered = export.render_recipients(items, lang=lang)
    return xlsx.xlsx_response(rendered, filename=filename, total=total, cap=cap)


@router.post("", response_model=PaymentRecipientOut, status_code=201)
async def create_recipient(
    body: PaymentRecipientIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(_WRITE)],
) -> PaymentRecipientOut:
    return await service.create(db, data=body, actor=actor)


@router.patch("/{recipient_id}", response_model=PaymentRecipientOut)
async def patch_recipient(
    recipient_id: uuid.UUID,
    body: PaymentRecipientPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(_WRITE)],
) -> PaymentRecipientOut:
    return await service.update(db, recipient_id=recipient_id, data=body, actor=actor)
