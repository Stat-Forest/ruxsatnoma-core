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
from app.modules.permits.permissions import PERMITS_ISSUE, PERMITS_SIGN
from app.modules.permits.schemas import PermitOut, PermitSignatureOut, PermitSignIn

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


@router.post("/permits/{permit_id}/signatures")
async def sign_permit(
    permit_id: uuid.UUID,
    payload: PermitSignIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PERMITS_SIGN))],
) -> PermitSignatureOut:
    """Attach one of the permit's four ERI signatures (C11). 200 with the
    permit's status and whatever is still missing; once nothing is missing the
    permit is ACTIVE, `issued_at` is stamped and the application has moved to
    PERMIT_ISSUED (ruling 18).

    200, not 201: the client does not get a URL for the signature it just made —
    `GET /signatures?object_type=permit&object_id=…` (3.8's own route) lists
    them, and what this answers is the state of the PERMIT.

    `permits.sign` is held by all four signatory roles (migration 0019), so this
    dependency only answers "may this role sign anything at all". WHICH of the
    four lines this particular actor may sign is `signers.py`'s map, checked in
    the service before the signature is taken — 403 `ERR-ACL-001` with
    `details.reason = "signer_not_authorized"` (lesson: a permission answers "at
    all", a second check answers "on what"). 409 `ERR-PERM-001` when the permit
    is not awaiting signatures; 422 `ERR-SIGN-001` when the envelope does not
    verify, in which case the attempt is still stored as evidence (3.8 ruling 8).

    No `Idempotency-Key`, for the same reason issuance needs none: a replay is
    refused by `uq_signatures_valid_purpose`, which `sign()` reports as 409
    `ERR-SIGN-002` — a permanent guarantee rather than one bounded by a replay
    window.
    """
    permit = await service.add_signature(
        db, permit_id, purpose=payload.purpose, pkcs7=payload.pkcs7, user=actor
    )
    # `model_validate`, not the constructor: `permits.status` is a plain `str`
    # column and `PermitStatus` is a `Literal`, so pydantic checks the membership
    # at runtime here rather than the call site suppressing a type error — the
    # same reason `PermitOut` is built by validation and not by hand.
    return PermitSignatureOut.model_validate(
        {
            "status": permit.status,
            "missing_signatures": await service.missing_signatures(db, permit.id),
        }
    )
