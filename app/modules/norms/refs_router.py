"""Read routes are open to any authenticated user (a front-end has to explain a
calculation); write routes are gated by the tariff permissions, which live with
the central office (decision #32).

`/publish` and `/archive` both accept anyone holding either `TARIFFS_MANAGE` or
`TARIFFS_PUBLISH` (migration 0011 grants `central_admin` both at once, on
purpose). The widened dependency is a ROUTING decision, not the gate: it exists
so a maker reaches a DOMAIN answer instead of a bare 403 — `TARIFFS_PUBLISH`
alone would 403 a maker before `service.publish_versioned`'s own
`not_maker_checker` refusal ever ran, which is not what
`test_a_maker_creates_a_draft_and_cannot_publish_it` (a `tariffs_maker_client`,
`TARIFFS_MANAGE` only) exercises. Both routes therefore carry their real gate
in the SERVICE, and both refuse `ERR-ACL-001` via `_holds_tariffs_publish`:

- `service.publish_versioned` requires it for every publication, after the
  `not_draft`/`not_maker_checker` refusals. The identity check alone was never
  the control (C1, final review): two DIFFERENT makers satisfy it, and a
  migration-seeded row (`created_by IS NULL`) skips it outright.
- `service.archive_versioned` requires it only when the row being archived is
  already `published` — archiving is a single-actor action with no identity to
  compare, and taking a row IN FORCE out of force is the one-person change
  maker-checker exists to prevent. Discarding one's own DRAFT stays a maker's
  call with no second person, which the route dependency alone cannot tell
  apart from archiving a published row."""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.errors import err
from app.core.schemas import Page
from app.core.time import business_today
from app.modules.admin import repo as admin_repo
from app.modules.auth.deps import get_current_user, require_any_permission, require_permission
from app.modules.auth.models import User
from app.modules.norms import repo, service
from app.modules.norms.permissions import TARIFFS_MANAGE, TARIFFS_PUBLISH
from app.modules.norms.schemas import (
    PublishOut,
    RuleParameterIn,
    RuleParameterOut,
    RuleParameterPatch,
    TariffIn,
    TariffOut,
    TariffPatch,
)

router = APIRouter(tags=["norms"])


def _paged(offset: int, limit: int) -> tuple[int, int]:
    """This module's own repo functions take `limit`/`offset` (not the
    project's usual `PageParams`), because they are shared with future
    non-HTTP callers (the calculator) that have no notion of a page number —
    `Page[T]` still needs one for its envelope, so it is derived here."""
    return offset // limit + 1, limit


@router.get("/rule-parameters", response_model=Page[RuleParameterOut])
async def list_parameters(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
    code: str | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    items, total = await repo.list_parameters(
        db, code=code, status=status, limit=limit, offset=offset
    )
    page, page_size = _paged(offset, limit)
    return Page[RuleParameterOut](
        items=[RuleParameterOut.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/rule-parameters", response_model=RuleParameterOut, status_code=201)
async def create_parameter(
    payload: RuleParameterIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(TARIFFS_MANAGE))],
) -> Any:
    return await service.create_versioned(db, service.PARAMETER, payload, actor=actor)


@router.patch("/rule-parameters/{parameter_id}", response_model=RuleParameterOut)
async def update_parameter(
    parameter_id: uuid.UUID,
    payload: RuleParameterPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(TARIFFS_MANAGE))],
) -> Any:
    return await service.update_versioned(db, service.PARAMETER, parameter_id, payload, actor=actor)


@router.post("/rule-parameters/{parameter_id}/publish", response_model=PublishOut)
async def publish_parameter(
    parameter_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(TARIFFS_PUBLISH, TARIFFS_MANAGE))],
) -> Any:
    row, warnings = await service.publish_versioned(
        db, service.PARAMETER, parameter_id, actor=actor
    )
    return {"item": RuleParameterOut.model_validate(row), "warnings": warnings}


@router.post("/rule-parameters/{parameter_id}/archive", response_model=RuleParameterOut)
async def archive_parameter(
    parameter_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(TARIFFS_PUBLISH, TARIFFS_MANAGE))],
) -> Any:
    return await service.archive_versioned(db, service.PARAMETER, parameter_id, actor=actor)


# --- tariffs -----------------------------------------------------------------


async def _resolve_activity_type_id(
    db: AsyncSession, *, activity_code: str | None, activity_type_id: uuid.UUID | None
) -> uuid.UUID | None:
    """`activity_code` is the human-friendly query key; resolved through
    `admin.repo.list_activity_types` rather than a direct `activity_types`
    query, per the module boundary (backend/CLAUDE.md 'Reference data').
    `activity_type_id` wins when both are given; neither given means "every
    activity" (no filter)."""
    if activity_type_id is not None:
        return activity_type_id
    if activity_code is None:
        return None
    for activity_type in await admin_repo.list_activity_types(db):
        if activity_type.code == activity_code:
            return activity_type.id
    raise err("ERR-SYS-003", details={"activity_code": activity_code})


@router.get("/tariffs", response_model=Page[TariffOut])
async def list_tariffs(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
    activity_code: str | None = None,
    activity_type_id: uuid.UUID | None = None,
    on_date: date | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    resolved_activity_type_id = await _resolve_activity_type_id(
        db, activity_code=activity_code, activity_type_id=activity_type_id
    )
    items, total = await repo.list_tariffs(
        db,
        activity_type_id=resolved_activity_type_id,
        on_date=on_date or business_today(),
        status=status,
        limit=limit,
        offset=offset,
    )
    page, page_size = _paged(offset, limit)
    return Page[TariffOut](
        items=[TariffOut.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/tariffs", response_model=TariffOut, status_code=201)
async def create_tariff(
    payload: TariffIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(TARIFFS_MANAGE))],
) -> Any:
    return await service.create_versioned(db, service.TARIFF, payload, actor=actor)


@router.patch("/tariffs/{tariff_id}", response_model=TariffOut)
async def update_tariff(
    tariff_id: uuid.UUID,
    payload: TariffPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(TARIFFS_MANAGE))],
) -> Any:
    return await service.update_versioned(db, service.TARIFF, tariff_id, payload, actor=actor)


@router.post("/tariffs/{tariff_id}/publish", response_model=PublishOut)
async def publish_tariff(
    tariff_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(TARIFFS_PUBLISH, TARIFFS_MANAGE))],
) -> Any:
    row, warnings = await service.publish_versioned(db, service.TARIFF, tariff_id, actor=actor)
    return {"item": TariffOut.model_validate(row), "warnings": warnings}


@router.post("/tariffs/{tariff_id}/archive", response_model=TariffOut)
async def archive_tariff(
    tariff_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(TARIFFS_PUBLISH, TARIFFS_MANAGE))],
) -> Any:
    return await service.archive_versioned(db, service.TARIFF, tariff_id, actor=actor)
