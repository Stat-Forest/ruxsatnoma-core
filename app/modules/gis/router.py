"""Contour identity, draft versions, and the version lifecycle. Creating and
editing (`POST/PATCH .../contours`, `.../versions`) requires `CONTOURS_MANAGE`
(the GIS specialist draws, edits and imports, but never approves — that
permission gate matches `gis_client` in the test fixtures); `submit-review` is
`CONTOURS_MANAGE` too (the specialist hands their own draft on), as is
`return-to-draft` (they take it back), while `approve`/`publish`/`archive`
and `return-to-review` require `CONTOURS_APPROVE` (the rahbar —
`rahbar_client` in the tests). `POST .../split` (decision #91) is
`CONTOURS_MANAGE` too — the specialist splits, exactly the way they draw and
edit; the two resulting drafts go through approval like any other new
version. Every write below is ALSO zone-scoped through `service._assert_in_zone`,
a separate gate from the permission check (lesson: 'Zone scoping is not a
permission check — a read path needs both')."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files, xlsx
from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.core.time import business_today
from app.modules.auth.deps import get_current_user, require_any_permission, require_permission
from app.modules.auth.models import User
from app.modules.gis import checks, export, kmz, service
from app.modules.gis.models import VERSION_STATUSES
from app.modules.gis.permissions import CONTOURS_APPROVE, CONTOURS_MANAGE
from app.modules.gis.schemas import (
    ApproveIn,
    ChecksOut,
    ContourCardOut,
    ContourIn,
    ContourListItem,
    ContourOut,
    ContourPatch,
    FeatureCollectionOut,
    SplitIn,
    SplitOut,
    SplitPieceOut,
    VersionDetailOut,
    VersionIn,
    VersionOut,
    VersionPatch,
)

# `?status=` on the version list below — same shape as
# `payments.backoffice_router._STATUS_PATTERN` for the discrepancy register.
_VERSION_STATUS_PATTERN = "^(" + "|".join(VERSION_STATUSES) + ")$"

router = APIRouter(prefix="/gis", tags=["gis"])


@router.get("/contours", response_model=Page[ContourListItem])
async def list_contours(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    params: Annotated[PageParams, Depends()],
    organization_id: uuid.UUID | None = None,
    bbox: str | None = None,
    region_id: uuid.UUID | None = None,
) -> Page[ContourListItem]:
    """Reading published contours needs no permission at all (ruling 5): an
    applicant must be able to pick a plot the same way any authenticated user
    already reads `GET /gis/layers` (ruling 18).

    Paged with core's own `Page[T]`/`PageParams` (design/03: `?page=1&
    page_size=20`, max 100), the same envelope `/admin/users` uses — this list
    was unbounded, and an applicant picking a plot would have received every
    published contour in the country."""
    items, total = await service.list_contours(
        db,
        organization_id=organization_id,
        bbox=bbox,
        region_id=region_id,
        params=params,
        actor=user,
    )
    return Page[ContourListItem](
        items=[ContourListItem.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/contours/export.xlsx")
async def export_contours_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    lang: xlsx.Lang = "uz_latn",
    organization_id: uuid.UUID | None = None,
    bbox: str | None = None,
    region_id: uuid.UUID | None = None,
) -> Response:
    """`GET /gis/contours` as a spreadsheet (stage 13, ruling #204): the
    same filters, the same zone scoping, every matching row up to the
    configured cap. NO geometry — this is an attributes register, exactly
    like the list it mirrors. Declared before `/contours/{contour_id}` on
    purpose — `export.xlsx` is not a UUID, and the 422 the UUID parser
    would answer is a worse error than a 404."""
    items, total, cap = await export.rows_contours(
        db, actor=user, lang=lang, organization_id=organization_id, bbox=bbox, region_id=region_id
    )
    filename = f"konturlar-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render_contours(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.get("/contours/features", response_model=FeatureCollectionOut)
async def list_contour_features(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    organization_id: uuid.UUID | None = None,
    bbox: str | None = None,
    region_id: uuid.UUID | None = None,
) -> FeatureCollectionOut:
    """The published contour layer as GeoJSON — what a map draws before the
    applicant has picked anything. `GET /gis/contours` above answers the same
    contours as a paged LIST with no geometry; this answers them as a
    collection with geometry and no paging, because a viewport is not a page.

    **This route must stay ABOVE `/contours/{contour_id}`.** FastAPI matches in
    declaration order, so with the two swapped the literal `features` is read
    as a `uuid.UUID` path parameter and every call to this endpoint is a 422
    that mentions a contour id nobody sent.

    Send a `?bbox=` — without one this is every published contour the caller
    may see, and `truncated` in the response says when that hit the cap.
    """
    return FeatureCollectionOut.model_validate(
        await service.list_contour_features(
            db, bbox=bbox, organization_id=organization_id, region_id=region_id, actor=user
        )
    )


@router.get("/contours/{contour_id}/export.kmz")
async def export_contour_kmz(
    contour_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    lang: xlsx.Lang = "uz_latn",
) -> Response:
    """The card's published boundary as a KMZ file (Odilxon, 2026-09-13):
    what the application and permit cards' «KMZ yuklab olish» button
    downloads. Same reader as the card, so the same people see the same
    polygon; 404 `ERR-GIS-007` when the contour has no geometry to give
    (decision #178) — the button hides on that card, and a direct call is
    told why rather than handed an empty file."""
    data, filename = await service.contour_kmz(db, contour_id, actor=user, lang=lang)
    return Response(
        content=data,
        media_type=kmz.MEDIA_TYPE,
        headers={
            "Content-Disposition": files.content_disposition("attachment", filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/contours/{contour_id}")
async def get_contour_card(
    contour_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> ContourCardOut:
    card = await service.contour_card(db, contour_id, actor=user)
    return ContourCardOut.model_validate(card)


@router.post("/contours", status_code=201)
async def create_contour(
    payload: ContourIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
) -> ContourOut:
    contour = await service.create_contour(
        db,
        layer_id=payload.layer_id,
        organization_id=payload.organization_id,
        number=payload.number,
        kind=payload.kind,
        parent_id=payload.parent_id,
        actor=user,
    )
    return ContourOut.model_validate(contour, from_attributes=True)


@router.patch("/contours/{contour_id}")
async def patch_contour(
    contour_id: uuid.UUID,
    payload: ContourPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
) -> ContourOut:
    contour = await service.update_contour(
        db, contour_id, actor=user, **payload.model_dump(exclude_unset=True)
    )
    return ContourOut.model_validate(contour, from_attributes=True)


@router.post("/contours/{parent_id}/split", status_code=201)
async def split_contour(
    parent_id: uuid.UUID,
    payload: SplitIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
) -> SplitOut:
    """Decision #91: one parent, two subcontours, atomically — replaces the
    adminka's own client-composed `createContour` + `createVersion`, twice.
    See `service.split_contour`'s own docstring for the full refusal list and
    why the geometry itself stays client-computed."""
    child_a, version_a, child_b, version_b = await service.split_contour(
        db,
        parent_id,
        actor=user,
        piece_a={
            "number": payload.piece_a.number,
            "geom": payload.piece_a.geom,
            "declared_area_ha": payload.piece_a.declared_area_ha,
        },
        piece_b={
            "number": payload.piece_b.number,
            "geom": payload.piece_b.geom,
            "declared_area_ha": payload.piece_b.declared_area_ha,
        },
        source=payload.source,
        accuracy_m=payload.accuracy_m,
        survey_date=payload.survey_date,
        effective_from=payload.effective_from,
    )
    return SplitOut(
        parent_id=parent_id,
        piece_a=SplitPieceOut(
            contour=ContourOut.model_validate(child_a, from_attributes=True),
            version=VersionOut.model_validate(version_a, from_attributes=True),
        ),
        piece_b=SplitPieceOut(
            contour=ContourOut.model_validate(child_b, from_attributes=True),
            version=VersionOut.model_validate(version_b, from_attributes=True),
        ),
    )


@router.get("/contours/{contour_id}/versions", response_model=Page[VersionOut])
async def list_versions(
    contour_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    # Both roles that can act on a version need to find it: the specialist
    # tracking their own draft/review submission (`CONTOURS_MANAGE`) and the
    # rahbar who must approve it (`CONTOURS_APPROVE`) — task defect 4a.
    user: Annotated[User, Depends(require_any_permission(CONTOURS_MANAGE, CONTOURS_APPROVE))],
    params: Annotated[PageParams, Depends()],
    status: Annotated[str | None, Query(pattern=_VERSION_STATUS_PATTERN)] = None,
) -> Any:
    items, total = await service.list_versions(
        db, contour_id, status=status, params=params, actor=user
    )
    return Page[VersionOut](
        items=[VersionOut.model_validate(item, from_attributes=True) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/contours/{contour_id}/versions/{version_id}", response_model=VersionDetailOut)
async def get_version(
    contour_id: uuid.UUID,
    version_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_any_permission(CONTOURS_MANAGE, CONTOURS_APPROVE))],
) -> Any:
    detail = await service.version_detail(db, contour_id, version_id, actor=user)
    return VersionDetailOut.model_validate(detail)


@router.post("/contours/{contour_id}/versions", status_code=201)
async def create_version(
    contour_id: uuid.UUID,
    payload: VersionIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
) -> VersionOut:
    version = await service.create_version(
        db,
        contour_id,
        actor=user,
        geojson=payload.geom,
        source=payload.source,
        declared_area_ha=payload.declared_area_ha,
        accuracy_m=payload.accuracy_m,
        survey_date=payload.survey_date,
        effective_from=payload.effective_from,
    )
    return VersionOut.model_validate(version, from_attributes=True)


@router.patch("/contours/{contour_id}/versions/{version_id}")
async def patch_version(
    contour_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: VersionPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
) -> VersionOut:
    version = await service.update_version(
        db, contour_id, version_id, actor=user, **payload.model_dump(exclude_unset=True)
    )
    return VersionOut.model_validate(version, from_attributes=True)


@router.post("/contours/{contour_id}/versions/{version_id}/checks")
async def check_version(
    contour_id: uuid.UUID,
    version_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
) -> ChecksOut:
    results = await service.run_version_checks(db, contour_id, version_id, actor=user)
    return ChecksOut.model_validate({"checks": results, "blocked": checks.is_blocked(results)})


@router.post("/contours/{contour_id}/versions/{version_id}/submit-review")
async def submit_review(
    contour_id: uuid.UUID,
    version_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
) -> VersionOut:
    version = await service.submit_review(db, version_id, actor=user)
    return VersionOut.model_validate(version, from_attributes=True)


@router.post("/contours/{contour_id}/versions/{version_id}/approve")
async def approve_version(
    contour_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: ApproveIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_APPROVE))],
) -> VersionOut:
    version = await service.approve_version(
        db, version_id, actor=user, approval_doc_id=payload.approval_doc_id
    )
    return VersionOut.model_validate(version, from_attributes=True)


@router.post("/contours/{contour_id}/versions/{version_id}/publish")
async def publish_version(
    contour_id: uuid.UUID,
    version_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_APPROVE))],
) -> VersionOut:
    version = await service.publish_version(db, version_id, actor=user)
    return VersionOut.model_validate(version, from_attributes=True)


@router.post("/contours/{contour_id}/versions/{version_id}/return-to-review")
async def return_to_review(
    contour_id: uuid.UUID,
    version_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_APPROVE))],
) -> VersionOut:
    """approved -> review: the approver takes their own approval back so the
    specialist can fix a version a publish check blocked."""
    version = await service.return_to_review(db, version_id, actor=user)
    return VersionOut.model_validate(version, from_attributes=True)


@router.post("/contours/{contour_id}/versions/{version_id}/return-to-draft")
async def return_to_draft(
    contour_id: uuid.UUID,
    version_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_MANAGE))],
) -> VersionOut:
    """review -> draft: the specialist takes their own submission back — only a
    draft is editable, so this is what makes a blocked version fixable."""
    version = await service.return_to_draft(db, version_id, actor=user)
    return VersionOut.model_validate(version, from_attributes=True)


@router.post("/contours/{contour_id}/versions/{version_id}/archive")
async def archive_version(
    contour_id: uuid.UUID,
    version_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(CONTOURS_APPROVE))],
) -> VersionOut:
    version = await service.archive_version(db, version_id, actor=user)
    return VersionOut.model_validate(version, from_attributes=True)
