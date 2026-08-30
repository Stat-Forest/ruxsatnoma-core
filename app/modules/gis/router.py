"""Contour identity, draft versions, and the version lifecycle. Creating and
editing (`POST/PATCH .../contours`, `.../versions`) requires `CONTOURS_MANAGE`
(the GIS specialist draws, edits and imports, but never approves — that
permission gate matches `gis_client` in the test fixtures); `submit-review` is
`CONTOURS_MANAGE` too (the specialist hands their own draft on), as is
`return-to-draft` (they take it back), while `approve`/`publish`/`archive`
and `return-to-review` require `CONTOURS_APPROVE` (the rahbar —
`rahbar_client` in the tests). Every write below is ALSO zone-scoped through
`service._assert_in_zone`, a separate gate from the permission check (lesson:
'Zone scoping is not a permission check — a read path needs both')."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User
from app.modules.gis import checks, service
from app.modules.gis.permissions import CONTOURS_APPROVE, CONTOURS_MANAGE
from app.modules.gis.schemas import (
    ApproveIn,
    ChecksOut,
    ContourCardOut,
    ContourIn,
    ContourListItem,
    ContourOut,
    ContourPatch,
    VersionIn,
    VersionOut,
    VersionPatch,
)

router = APIRouter(prefix="/gis", tags=["gis"])


@router.get("/contours", response_model=Page[ContourListItem])
async def list_contours(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    params: Annotated[PageParams, Depends()],
    organization_id: uuid.UUID | None = None,
    bbox: str | None = None,
) -> Page[ContourListItem]:
    """Reading published contours needs no permission at all (ruling 5): an
    applicant must be able to pick a plot the same way any authenticated user
    already reads `GET /gis/layers` (ruling 18).

    Paged with core's own `Page[T]`/`PageParams` (design/03: `?page=1&
    page_size=20`, max 100), the same envelope `/admin/users` uses — this list
    was unbounded, and an applicant picking a plot would have received every
    published contour in the country."""
    items, total = await service.list_contours(
        db, organization_id=organization_id, bbox=bbox, params=params, actor=user
    )
    return Page[ContourListItem](
        items=[ContourListItem.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
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
