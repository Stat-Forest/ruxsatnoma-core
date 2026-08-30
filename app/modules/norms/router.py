"""The norm itself: Draft -> Review -> Approved -> Published -> Archived
(VMQ 689). Read is open to any authenticated user (a front-end has to explain
a calculation, same reasoning as `refs_router.py`'s own module docstring);
every write route is BOTH permission-gated here AND zone-checked inside
`service` (`_assert_norm_zone`, called from every one of the service
functions below) — a per-endpoint guard is not a root fix when several
actions share the same precondition (lesson).

`publish` carries one extra rule beyond its own `NORMS_PUBLISH` permission:
`service.publish_norm` separately refuses a zone-scoped actor while
`norms_publish_scope == "central"` (ruling 16) — the route dependency alone
cannot express that, since it depends on a runtime setting, not a fixed role."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.schemas import Page
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User
from app.modules.norms import repo, service
from app.modules.norms.permissions import NORMS_APPROVE, NORMS_MANAGE, NORMS_PUBLISH
from app.modules.norms.schemas import NormApproveIn, NormIn, NormOut, NormPatch

router = APIRouter(tags=["norms"])


@router.get("/norms", response_model=Page[NormOut])
async def list_norms(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
    contour_id: uuid.UUID | None = None,
    activity_type_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    items, total = await repo.list_norms(
        db,
        contour_id=contour_id,
        activity_type_id=activity_type_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return Page[NormOut](
        items=[NormOut.model_validate(item) for item in items],
        total=total,
        page=offset // limit + 1,
        page_size=limit,
    )


@router.get("/norms/{norm_id}", response_model=NormOut)
async def get_norm(
    norm_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
) -> Any:
    return await service.get_norm(db, norm_id)


@router.post("/norms", response_model=NormOut, status_code=201)
async def create_norm(
    payload: NormIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(NORMS_MANAGE))],
) -> Any:
    return await service.create_norm(db, payload, actor=actor)


@router.patch("/norms/{norm_id}", response_model=NormOut)
async def update_norm(
    norm_id: uuid.UUID,
    payload: NormPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(NORMS_MANAGE))],
) -> Any:
    return await service.update_norm(db, norm_id, payload, actor=actor)


@router.post("/norms/{norm_id}/submit-review", response_model=NormOut)
async def submit_norm_review(
    norm_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(NORMS_MANAGE))],
) -> Any:
    return await service.submit_norm_review(db, norm_id, actor=actor)


@router.post("/norms/{norm_id}/return-to-draft", response_model=NormOut)
async def return_norm_to_draft(
    norm_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(NORMS_MANAGE))],
) -> Any:
    return await service.return_norm_to_draft(db, norm_id, actor=actor)


@router.post("/norms/{norm_id}/approve", response_model=NormOut)
async def approve_norm(
    norm_id: uuid.UUID,
    payload: NormApproveIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(NORMS_APPROVE))],
) -> Any:
    return await service.approve_norm(db, norm_id, payload.approval_doc_id, actor=actor)


@router.post("/norms/{norm_id}/publish", response_model=NormOut)
async def publish_norm(
    norm_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(NORMS_PUBLISH))],
) -> Any:
    return await service.publish_norm(db, norm_id, actor=actor)


@router.post("/norms/{norm_id}/return-to-review", response_model=NormOut)
async def return_norm_to_review(
    norm_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(NORMS_PUBLISH))],
) -> Any:
    return await service.return_norm_to_review(db, norm_id, actor=actor)


@router.post("/norms/{norm_id}/archive", response_model=NormOut)
async def archive_norm(
    norm_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(NORMS_APPROVE))],
) -> Any:
    return await service.archive_norm(db, norm_id, actor=actor)
