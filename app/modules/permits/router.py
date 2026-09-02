"""Permit routes.

`POST /applications/{id}/permit` lives on the applications path because
`design/03` puts it there and a front-end already reads that contract — but the
MODULE is `permits`, and this is its router. A path is not a module boundary:
`applications` is level 3 and may not ask whether a permit exists, so the route
that creates one cannot live in its router.

No `Idempotency-Key` on this POST, deliberately (`app/core/idempotency.py` is the
mechanism 3.6a's import route uses). `permits.application_id` is unique and the
service refuses a second issuance with `ERR-PERM-001`, so a replayed request is
answered as a domain conflict rather than producing a second numbered document —
which is a stronger guarantee than a replay window, and one that also holds for a
second request that is not a replay at all.

Importing `permissions` below is what registers this module's four codes (the
same idiom every other router uses). Migration 0019 grants them to four roles, and
`tests/test_permissions_registry.py` fails on a granted code the registry never
learned — which is why `app/main.py` carried a stand-in import until this file
existed.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User
from app.modules.permits import service
from app.modules.permits.permissions import PERMITS_ISSUE
from app.modules.permits.schemas import PermitOut

router = APIRouter(tags=["permits"])


@router.post("/applications/{application_id}/permit", status_code=201)
async def issue_permit(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PERMITS_ISSUE))],
) -> PermitOut:
    """Form the permit for a PAID application: 201 with the numbered document
    awaiting its four signatures.

    422 `ERR-PAY-001` when the application is not PAID — and that refusal is
    recorded as RI-10 (`tz/10`: CRITICAL, immediate) before the exception that
    explains it. 409 `ERR-PERM-001` when this application already has a permit.
    403 `ERR-ACL-002` when the plot is outside the caller's zone, which the
    service checks in addition to the permission gate above.
    """
    permit = await service.issue(db, application_id, actor=actor)
    return PermitOut.model_validate(permit)
