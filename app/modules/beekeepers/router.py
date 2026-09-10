"""The beekeepers register HTTP API — every route gated on `beekeepers.manage`
(ruling #182: one central role, the Union's own employee, holds it and
nothing else). Never a DELETE route: `POST /{id}/remove` is the only way a
row stops being active, and it always leaves the row in place."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import xlsx
from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.core.time import business_today
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User
from app.modules.beekeepers import export, service
from app.modules.beekeepers.permissions import BEEKEEPERS_MANAGE
from app.modules.beekeepers.schemas import (
    BeekeeperCreateIn,
    BeekeeperLookupOut,
    BeekeeperOut,
    BeekeeperPatchIn,
    BeekeeperRemoveIn,
)

router = APIRouter(tags=["beekeepers"])


@router.get("/beekeepers", response_model=Page[BeekeeperOut])
async def list_beekeepers(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[PageParams, Depends()],
    _: Annotated[User, Depends(require_permission(BEEKEEPERS_MANAGE))],
    q: str | None = Query(default=None, max_length=200),
    status: str | None = None,
) -> Page[BeekeeperOut]:
    return await service.list_beekeepers(db, params=params, q=q, status=status)


@router.get("/beekeepers/export.xlsx")
async def export_beekeepers_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_permission(BEEKEEPERS_MANAGE))],
    lang: xlsx.Lang = "uz_latn",
    q: str | None = Query(default=None, max_length=200),
    status: str | None = None,
) -> Response:
    """`GET /beekeepers` as a spreadsheet (stage 13, ruling #204): the same
    filters, the same scope (ruling R2 — no zone here, ruling #182's single
    central role), every matching row up to the configured cap."""
    items, total, cap = await export.rows(db, q=q, status=status)
    filename = f"asalarichilar-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.get("/beekeepers/lookup", response_model=BeekeeperLookupOut)
async def lookup_beekeeper(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(BEEKEEPERS_MANAGE))],
    pinfl: Annotated[str, Query(pattern=r"^[0-9]{14}$")],
) -> Any:
    return await service.lookup_by_pinfl(db, pinfl=pinfl, actor=actor)


@router.post("/beekeepers", response_model=BeekeeperOut, status_code=201)
async def create_beekeeper(
    body: BeekeeperCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(BEEKEEPERS_MANAGE))],
) -> Any:
    return await service.create_beekeeper(db, data=body, actor=actor)


@router.patch("/beekeepers/{beekeeper_id}", response_model=BeekeeperOut)
async def patch_beekeeper(
    beekeeper_id: uuid.UUID,
    body: BeekeeperPatchIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(BEEKEEPERS_MANAGE))],
) -> Any:
    return await service.patch_beekeeper(db, beekeeper_id=beekeeper_id, data=body, actor=actor)


@router.post("/beekeepers/{beekeeper_id}/remove", response_model=BeekeeperOut)
async def remove_beekeeper(
    beekeeper_id: uuid.UUID,
    body: BeekeeperRemoveIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(BEEKEEPERS_MANAGE))],
) -> Any:
    return await service.remove_beekeeper(db, beekeeper_id=beekeeper_id, data=body, actor=actor)
