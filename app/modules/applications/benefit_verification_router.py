"""Routes for a benefit claim's verify/reject pair — a sibling of `router.py`,
mounted under the SAME `/applications` prefix: a benefit claim is a field of
the application, not a rival resource, so `/applications/benefit-
verifications/*` reads as "benefit verifications, of applications".

**Ruling #182 moved this from a central, country-wide office to the leshoz's
own review.** `require_permission(BENEFITS_VERIFY)` is the FIRST gate on
every route below (`benefits.verify` now sits on `executor_staff`/
`executor_head`, migration `0053`) — the SECOND is the application's own
read/zone rule, enforced inside `benefit_verification.py`
(`flow._readable_application` for the read, `flow._assert_in_actor_zone` for
verify/reject), exactly like `GET /applications/{id}` and
`decision.approve`/`.reject`. **The list route this office used to carry,
`GET /applications/benefit-verifications`, is GONE**: a leshoz reviewer works
this claim from the application card it already reads, never a queue of its
own — retiring the "whole country, only claims" predicate #179 built and
#182 named for removal.

Registering the code happens by importing `permissions` from
`benefit_verification.py`'s own module (`app.modules.applications.
permissions`), already imported at process start by `router.py` — this file
adds no import of its own for that, the same "importing registers it" idiom
`router.py`'s own docstring states.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.modules.applications import benefit_verification as service
from app.modules.applications.permissions import BENEFITS_VERIFY
from app.modules.applications.schemas import (
    ApplicationOut,
    BenefitClaimDetailOut,
    BenefitClaimRejectIn,
)
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User

router = APIRouter(tags=["benefit-verification"])


@router.get("/applications/benefit-verifications/{application_id}")
async def get_benefit_claim(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(BENEFITS_VERIFY))],
) -> BenefitClaimDetailOut:
    """The claim plus its supporting document(s) — everything the reviewer
    needs to decide.

    404 `ERR-SYS-003` for an id that does not exist, for an application
    outside the caller's zone, and for a real application carrying no
    certificate-bearing claim — the same answer for all three, because
    anything else would make this route an application-existence oracle for
    a document full of personal data (`benefit_verification.py`'s own module
    docstring).
    """
    application, documents = await service.get_claim_detail(db, application_id, actor=actor)
    return BenefitClaimDetailOut.build(application, documents)


@router.post("/applications/benefit-verifications/{application_id}/verify")
async def verify_benefit_claim(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(BENEFITS_VERIFY))],
) -> ApplicationOut:
    """`pending -> verified`. 404 `ERR-SYS-003` for an id that does not exist
    or an application outside the caller's zone; 409 `ERR-APP-004`
    (`reason="not_in_review"`) when the application itself is not
    `IN_REVIEW`; 409 `ERR-APP-004` (`reason="not_pending"`) if this claim was
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
