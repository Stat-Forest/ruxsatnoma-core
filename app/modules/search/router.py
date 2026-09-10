"""`search` — a level-5 reader (design/01 rule 5). Every route requires
`search.use`; the zone restriction on top of it lives in `service.py`, never
here (same split every other module's router/service pair uses)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files, xlsx
from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.core.time import business_today
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User
from app.modules.search import export, service
from app.modules.search.permissions import SEARCH_USE
from app.modules.search.schemas import (
    ExportCreate,
    ExportJobOut,
    SavedFilterIn,
    SavedFilterOut,
    SavedFilterPatch,
    SearchKind,
    SearchResultOut,
)

router = APIRouter(tags=["search"])


@router.get("/search", response_model=Page[SearchResultOut])
async def search(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SEARCH_USE))],
    params: Annotated[PageParams, Depends()],
    kind: SearchKind,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    filter_status: Annotated[str | None, Query(alias="status")] = None,
    organization_id: uuid.UUID | None = None,
    activity_type_id: uuid.UUID | None = None,
    series: Annotated[str | None, Query(max_length=10)] = None,
) -> Page[SearchResultOut]:
    return await service.search(
        db,
        actor=actor,
        kind=kind,
        params=params,
        q=q,
        status=filter_status,
        organization_id=organization_id,
        activity_type_id=activity_type_id,
        series=series,
    )


@router.get("/search/export.xlsx")
async def export_search_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SEARCH_USE))],
    kind: SearchKind,
    lang: xlsx.Lang = "uz_latn",
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    filter_status: Annotated[str | None, Query(alias="status")] = None,
    organization_id: uuid.UUID | None = None,
    activity_type_id: uuid.UUID | None = None,
    series: Annotated[str | None, Query(max_length=10)] = None,
) -> Response:
    """`GET /search` as a spreadsheet — the same filters, the same zone
    (ruling R2: `service._rows_for`, the exact function `search()` itself
    calls), every matching row up to the configured cap. The PLAIN register
    export (ruling #204) beside the prosecutor's watermarked `POST
    /search/exports` (С22) — that route is untouched."""
    items, total, cap = await export.rows(
        db,
        actor=actor,
        lang=lang,
        kind=kind,
        q=q,
        status=filter_status,
        organization_id=organization_id,
        activity_type_id=activity_type_id,
        series=series,
    )
    filename = f"qidiruv-{kind}-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.post("/search/profiles", response_model=SavedFilterOut, status_code=status.HTTP_201_CREATED)
async def create_profile(
    data: SavedFilterIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SEARCH_USE))],
) -> SavedFilterOut:
    row = await service.create_saved_filter(db, actor, data)
    return SavedFilterOut.model_validate(row)


@router.get("/search/profiles", response_model=list[SavedFilterOut])
async def list_profiles(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SEARCH_USE))],
) -> list[SavedFilterOut]:
    rows = await service.list_saved_filters(db, actor)
    return [SavedFilterOut.model_validate(row) for row in rows]


@router.get("/search/profiles/{profile_id}", response_model=SavedFilterOut)
async def get_profile(
    profile_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SEARCH_USE))],
) -> SavedFilterOut:
    row = await service.get_saved_filter(db, actor, profile_id)
    return SavedFilterOut.model_validate(row)


@router.patch("/search/profiles/{profile_id}", response_model=SavedFilterOut)
async def update_profile(
    profile_id: uuid.UUID,
    patch: SavedFilterPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SEARCH_USE))],
) -> SavedFilterOut:
    row = await service.update_saved_filter(db, actor, profile_id, patch)
    return SavedFilterOut.model_validate(row)


@router.delete("/search/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    profile_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SEARCH_USE))],
) -> None:
    await service.delete_saved_filter(db, actor, profile_id)


# --- export (С22) ----------------------------------------------------------
#
# Gated on `search.use`, the SAME code `GET /search` itself requires — an
# export shows nothing a search result page does not already, so a second
# permission code would only be a second place for the two to drift apart
# (`.claude/lessons.md` "An access rule has ONE source").


@router.post("/search/exports", response_model=ExportJobOut, status_code=status.HTTP_201_CREATED)
async def create_export(
    data: ExportCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SEARCH_USE))],
) -> ExportJobOut:
    job = await service.create_export(db, actor, data)
    return ExportJobOut.model_validate(job)


@router.get("/search/exports", response_model=list[ExportJobOut])
async def list_exports(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SEARCH_USE))],
) -> list[ExportJobOut]:
    rows = await service.list_export_jobs(db, actor)
    return [ExportJobOut.model_validate(row) for row in rows]


@router.get("/search/exports/{job_id}", response_model=ExportJobOut)
async def get_export(
    job_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SEARCH_USE))],
) -> ExportJobOut:
    job = await service.get_export_job(db, actor, job_id)
    return ExportJobOut.model_validate(job)


@router.get("/search/exports/{job_id}/file")
async def download_export(
    job_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(SEARCH_USE))],
) -> Response:
    job, data = await service.get_export_file(db, actor, job_id)
    media_type = service.EXPORT_MEDIA_TYPE[job.format]
    filename = f"export-{job.kind}-{job.id}.{job.format}"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": files.content_disposition("attachment", filename),
            "X-Content-Type-Options": "nosniff",
        },
    )
