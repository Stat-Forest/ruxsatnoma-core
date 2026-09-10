"""GET /refs/*: form dictionaries for every authenticated user (ruling 10).

No permission code and no zone filtering — these are catalogs, not business objects.
The one exception is `PATCH /activity-types/{id}` (ruling #139): the hard catalog's
one edit is gated behind `admin.classifiers.manage`, because unlike every read here
it changes what the catalog says.
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import xlsx
from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.core.time import business_today
from app.modules.admin import export, repo, service
from app.modules.admin.permissions import CLASSIFIERS_MANAGE
from app.modules.admin.schemas import (
    ActivityTypeOut,
    ActivityTypePatch,
    ClassifierItemOut,
    DistrictOut,
    LivestockTypeOut,
    OrganizationOut,
    RegionOut,
)
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User

router = APIRouter(prefix="/refs", tags=["refs"], dependencies=[Depends(get_current_user)])


@router.get("/regions", response_model=list[RegionOut])
async def regions(db: Annotated[AsyncSession, Depends(get_db)]):
    return await repo.list_regions(db)


@router.get("/districts", response_model=list[DistrictOut])
async def districts(
    db: Annotated[AsyncSession, Depends(get_db)], region_id: uuid.UUID | None = None
):
    return await repo.list_districts(db, region_id)


@router.get("/organizations", response_model=Page[OrganizationOut])
async def organizations(
    db: Annotated[AsyncSession, Depends(get_db)],
    page: Annotated[PageParams, Depends()],
    parent_id: uuid.UUID | None = None,
    kind: str | None = None,
    region_id: uuid.UUID | None = None,
    status: str = "active",
) -> Page[OrganizationOut]:
    rows, total = await repo.list_organizations(
        db,
        parent_id=parent_id,
        kind=kind,
        region_id=region_id,
        status=status,
        offset=page.offset,
        limit=page.page_size,
    )
    return Page[OrganizationOut](
        items=[OrganizationOut.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


@router.get("/organizations/export.xlsx")
async def export_organizations_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    lang: xlsx.Lang = "uz_latn",
    parent_id: uuid.UUID | None = None,
    kind: str | None = None,
    region_id: uuid.UUID | None = None,
    status: str = "active",
) -> Response:
    """`GET /refs/organizations` as a spreadsheet (stage 13, ruling #204):
    the same filters, no permission code and no zone filtering (ruling 10),
    every matching row up to the configured cap."""
    items, total, cap = await export.organizations_rows(
        db, lang=lang, parent_id=parent_id, kind=kind, region_id=region_id, status=status
    )
    filename = f"tashkilotlar-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render_organizations(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.get("/activity-types", response_model=list[ActivityTypeOut])
async def activity_types(db: Annotated[AsyncSession, Depends(get_db)]):
    return await repo.list_activity_types(db)


@router.patch("/activity-types/{activity_type_id}", response_model=ActivityTypeOut)
async def update_activity_type(
    activity_type_id: uuid.UUID,
    body: ActivityTypePatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(CLASSIFIERS_MANAGE))],
) -> ActivityTypeOut:
    """Ruling #139: the only write the hard catalog offers. `admin.classifiers.manage`,
    the same grant the other reference edits carry — this router's own module-level
    `get_current_user` dependency is a read gate and is not enough for a write."""
    row = await service.update_activity_type(
        db, activity_type_id=activity_type_id, patch=body, actor=actor
    )
    return ActivityTypeOut.model_validate(row, from_attributes=True)


@router.get("/livestock-types", response_model=list[LivestockTypeOut])
async def livestock_types(db: Annotated[AsyncSession, Depends(get_db)]):
    return await repo.list_livestock_types(db)


@router.get("/classifiers/{code}/items", response_model=list[ClassifierItemOut])
async def classifier_items(
    code: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    on_date: Annotated[date | None, Query()] = None,
):
    return await service.classifier_items_by_code(db, code, on_date=on_date)
