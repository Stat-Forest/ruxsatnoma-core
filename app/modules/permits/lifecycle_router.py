"""The permit's post-issuance lifecycle: suspend and resume (plan
`03.11b-permits-lifecycle`). Revocation is deliberately NOT here — ruling 14
makes it a bigger act than either of these two (`service.revoke` must also
revoke the permit's live forest tickets in the SAME transaction), so Task 4
owns that route on its own, in this same file, once it exists.

A separate router from `router.py`, mounted alongside it in `app/main.py`,
rather than a third section appended there: `router.py`'s own docstring
already describes what importing it registers (this module's four permission
codes), and this file is where every LIFECYCLE act after issuance belongs, so
a reader looking for "what can happen to a permit once it exists" has one
file to open rather than a growing tail on the issuance-and-signature one.

This file is shared by parallel branches working the rest of this stage — a
merge conflict here is expected, not a mistake; keep both sides.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.modules.auth.deps import require_permission
from app.modules.auth.models import User
from app.modules.permits import decisions, events, grounds
from app.modules.permits.permissions import PERMITS_MANAGE
from app.modules.permits.schemas import DecisionIn, PermitOut

router = APIRouter(tags=["permits"])

# Both share `permits.manage`, migration 0019's own reservation for "3.11b's
# suspend / resume / revoke / duplicate" (`permissions.py`'s own docstring
# names this stage by number, so no migration of this stage's own is needed
# to grant it). `decisions.decide` carries the whole order of checks
# (`ERR-ACL-002` before `ERR-ACL-001`, the per-act document requirement, the
# audited signer refusal); these routes are only the thin, per-act shell
# around it.


@router.post("/permits/{permit_id}/suspend")
async def suspend_permit(
    permit_id: uuid.UUID,
    payload: DecisionIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PERMITS_MANAGE))],
) -> PermitOut:
    """С13: suspend an ACTIVE permit on a named ground, with the leshoz head's
    ERI signature over the decision itself (`decisions.py`'s module docstring).

    A supporting document is required (`ERR-VAL-001`, `doc_file_required`) —
    PS-04's fire-danger restriction and every other suspension ground name an
    order behind them. 409 `ERR-PERM-001` when the permit is not `active`; 403
    `ERR-ACL-002` outside the caller's leshoz, `ERR-ACL-001` when the caller
    holds `permits.manage` but not `executor_head` OF this leshoz.
    """
    permit = await decisions.decide(
        db,
        permit_id,
        act=grounds.SUSPEND,
        to_status="suspended",
        data=payload,
        actor=actor,
        event_code=events.PERMIT_SUSPENDED,
    )
    return PermitOut.model_validate(permit)


@router.post("/permits/{permit_id}/resume")
async def resume_permit(
    permit_id: uuid.UUID,
    payload: DecisionIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PERMITS_MANAGE))],
) -> PermitOut:
    """С13: resume a SUSPENDED permit, back to `active`. No supporting document
    is required — PS-06 «сабаб бартараф этилди» is a fact about the world a
    document cannot add to; 409 `ERR-PERM-001` when the permit is not
    `suspended`.
    """
    permit = await decisions.decide(
        db,
        permit_id,
        act=grounds.RESUME,
        to_status="active",
        data=payload,
        actor=actor,
        event_code=events.PERMIT_RESUMED,
    )
    return PermitOut.model_validate(permit)
