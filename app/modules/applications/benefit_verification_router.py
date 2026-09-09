"""Routes for ruling #179's central benefit-verification office — a sibling
of `router.py`, mounted under the SAME `/applications` prefix: a benefit
claim is a field of the application, not a rival resource, so `/applications/
benefit-verifications/*` reads as "benefit verifications, of applications".

`require_permission(BENEFITS_VERIFY)` is the WHOLE gate on every route below
— no ownership check, no ABAC zone, on purpose (ruling #179: a central office,
several users, one shared country-wide queue). Registering the code happens
by importing `permissions` from `benefit_verification.py`'s own module
(`app.modules.applications.permissions`), already imported at process start
by `router.py` — this file adds no import of its own for that, the same
"importing registers it" idiom `router.py`'s own docstring states.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.modules.applications import benefit_verification as service
from app.modules.applications.permissions import BENEFITS_VERIFY
from app.modules.applications.schemas import (
    ApplicationOut,
    BenefitClaimDetailOut,
    BenefitClaimRejectIn,
    BenefitVerificationStatus,
)
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User

router = APIRouter(tags=["benefit-verification"])


@router.get("/applications/benefit-verifications")
async def list_benefit_claims(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(BENEFITS_VERIFY))],
    params: Annotated[PageParams, Depends()],
    verification_status: BenefitVerificationStatus | None = None,
) -> Page[ApplicationOut]:
    """Every application carrying a certificate-bearing benefit claim,
    country-wide — this role's whole surface (ruling #179).

    `verification_status`, when given, narrows to exactly that value;
    `not_required` is a valid value of the wire enum but can never match a
    row this office is allowed to see (`repo.CERTIFICATE_BEARING_STATUSES`
    excludes it), so passing it answers an EMPTY page rather than a 422 —
    `GET /applications`'s own "entitled to nothing gets an empty page, never
    a 403" rule, restated here for a filter instead of the caller's identity.
    """
    items, total = await service.list_claims(
        db,
        verification_status=verification_status,
        offset=params.offset,
        limit=params.page_size,
    )
    return Page[ApplicationOut](
        items=[ApplicationOut.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/applications/benefit-verifications/{application_id}")
async def get_benefit_claim(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(BENEFITS_VERIFY))],
) -> BenefitClaimDetailOut:
    """The claim plus its supporting document(s) — everything the office
    needs to decide.

    404 `ERR-SYS-003` for an id that does not exist AND for a real
    application carrying no certificate-bearing claim — the same answer,
    because anything else would make this route an application-existence
    oracle for a document full of personal data (`benefit_verification.py`'s
    own module docstring).
    """
    application, documents = await service.get_claim_detail(db, application_id)
    return BenefitClaimDetailOut.build(application, documents)


@router.post("/applications/benefit-verifications/{application_id}/verify")
async def verify_benefit_claim(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(BENEFITS_VERIFY))],
) -> ApplicationOut:
    """`pending -> verified`. 404 `ERR-SYS-003` on the same two cases as the
    read above; 409 `ERR-APP-004` (`reason="not_pending"`) if this claim was
    already decided."""
    application = await service.verify_claim(db, application_id, actor=actor)
    return ApplicationOut.model_validate(application)


@router.post("/applications/benefit-verifications/{application_id}/reject")
async def reject_benefit_claim(
    application_id: uuid.UUID,
    payload: BenefitClaimRejectIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(BENEFITS_VERIFY))],
) -> ApplicationOut:
    """`pending -> rejected`, with `payload.reason` MANDATORY at the wire
    (`BenefitClaimRejectIn`, `min_length=1`) — a 422 `ERR-VAL-001` for a
    missing or blank one, before this ever reaches the service. Same 404/409
    shape as `verify_benefit_claim` otherwise."""
    application = await service.reject_claim(db, application_id, actor=actor, reason=payload.reason)
    return ApplicationOut.model_validate(application)
