"""The permit's post-issuance lifecycle: suspend, resume, revoke and the
duplicate (нусха) register (plan `03.11b-permits-lifecycle`). Revocation is
the bigger of the first three (ruling 14 — `service.revoke` also revokes the
permit's live forest tickets in the SAME transaction), which is why it is a
route of its own rather than a third copy of the suspend/resume shape, even
though the route itself is just as thin. The duplicate register (Task 5) is
not a status change at all — no entry in `PERMIT_TRANSITIONS` moves for it —
and its two routes carry their own, DIFFERENT gate: see the section comment
above them.

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
from app.modules.applications.permissions import APPLICATIONS_CREATE
from app.modules.applications.schemas import ApplicationOut
from app.modules.auth.deps import get_current_user, require_any_permission, require_permission
from app.modules.auth.models import User
from app.modules.permits import service
from app.modules.permits.permissions import PERMITS_ISSUE, PERMITS_MANAGE
from app.modules.permits.schemas import (
    DecisionIn,
    DuplicateIn,
    DuplicateOut,
    ForestTicketIn,
    ForestTicketOut,
    PermitOut,
)

router = APIRouter(tags=["permits"])

# All three of the routes below share `permits.manage`, migration 0019's own
# reservation for "3.11b's suspend / resume / revoke / duplicate"
# (`permissions.py`'s own docstring names this stage by number, so no
# migration of this stage's own is needed to grant it). All three call
# `service.suspend`/`service.resume`/`service.revoke` rather than
# `decisions.decide` directly — the one cross-module path backend/CLAUDE.md
# requires ("Cross-module calls only via the other module's service"), and
# the same path stage 4.1's inspector-initiated suspension will reach with
# its own actor. Those service functions are themselves thin: `decisions.decide`
# carries the whole order of checks (`ERR-ACL-002` before `ERR-ACL-001`, the
# per-act document requirement, the audited signer refusal) — see that
# module's own docstring.


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
    permit = await service.suspend(db, permit_id, data=payload, actor=actor)
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
    permit = await service.resume(db, permit_id, data=payload, actor=actor)
    return PermitOut.model_validate(permit)


@router.post("/permits/{permit_id}/revoke")
async def revoke_permit(
    permit_id: uuid.UUID,
    payload: DecisionIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PERMITS_MANAGE))],
) -> PermitOut:
    """С13: cancel a permit for cause, from `active` or from `suspended`
    (`PERMIT_TRANSITIONS`) — terminal but for 4.7's `archived`. The permit's
    live forest ticket is revoked in the same transaction (ruling 14,
    `service.revoke`'s own docstring); no refund is created here (ruling 15).

    A supporting document is required (`ERR-VAL-001`, `doc_file_required`) —
    the same rule `suspend` carries, for the same reason: an authoritative act
    needs an order behind it. 409 `ERR-PERM-001` when the permit is neither
    `active` nor `suspended` (a repeat carries `from == to` in `details`); 403
    `ERR-ACL-002` outside the caller's leshoz, `ERR-ACL-001`
    `signer_not_authorized` when the caller holds `permits.manage` but not
    `executor_head` OF this leshoz; 422 `ERR-SIGN-001` when the envelope does
    not verify, in which case the permit is untouched and the attempt is
    stored as evidence.
    """
    permit = await service.revoke(db, permit_id, data=payload, actor=actor)
    return PermitOut.model_validate(permit)


# --- Task 5: the duplicate (нусха) register ----------------------------------
#
# A DIFFERENT gate from the three routes above, deliberately. `POST` is issued
# by the same hodim who forms the permit (`permits.issue`) as much as by the
# leshoz head (`permits.manage`) — a lost paper copy is routine clerical work,
# not an authoritative act on the permit's status, and no entry in
# `PERMIT_TRANSITIONS` moves for it. `GET` carries no permission dependency at
# all: its audience is `service._readable_permit`'s three sets (the holder, a
# required signer, a `permits.view_any` holder in zone), which is WIDER than
# either write permission on purpose — the same shape `router.py`'s own three
# read routes already use, for the same reason (a citizen holder has neither
# grant and still owns this document).


@router.post("/permits/{permit_id}/duplicates", status_code=201)
async def create_permit_duplicate(
    permit_id: uuid.UUID,
    payload: DuplicateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_any_permission(PERMITS_ISSUE, PERMITS_MANAGE))],
) -> DuplicateOut:
    """С13's нусха: register a copy of the permit's OWN stored PDF — never a
    re-render (`service.issue_duplicate`'s own docstring spells out why, and
    what a duplicate inherits unchanged: the original's QR, correct or not).

    409 `ERR-PERM-001` with `details.reason`: `"not_duplicable"` for a permit
    in `pending_signatures` or `archived`; `"no_document"` for one with no
    stored PDF at all (defensive — not reachable through normal issuance).
    """
    duplicate = await service.issue_duplicate(db, permit_id, data=payload, actor=actor)
    return DuplicateOut.model_validate(duplicate)


@router.get("/permits/{permit_id}/duplicates")
async def list_permit_duplicates(
    permit_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> list[DuplicateOut]:
    """The permit's whole register, newest first. 404 `ERR-SYS-003` for an id
    that does not exist or that this caller has no claim on — the same
    permit-existence-oracle avoidance `GET /permits/{id}` uses, through the
    identical function (`service._readable_permit`).
    """
    rows = await service.list_duplicates(db, permit_id, actor=actor)
    return [DuplicateOut.model_validate(row) for row in rows]


# --- Task 6: the forest ticket (ЧТ), ВМҚ 506 ----------------------------------
#
# Back to `permits.manage`, the same gate `suspend`/`resume`/`revoke` carry
# above — DELIBERATELY narrower than the duplicate pair just above it (ruling
# 2, the gate Task 6's own brief left unnamed): a ticket is issued and revoked
# by the leshoz that MANAGES the permit, not merely by whoever holds
# `permits.issue`. `GET` carries the SAME dependency as `POST`, unlike the
# duplicate register's open read: a forest ticket is management paperwork,
# not something this module opens to the permit's holder or its other
# signatories.


@router.post("/permits/{permit_id}/forest-tickets", status_code=201)
async def create_forest_ticket(
    permit_id: uuid.UUID,
    payload: ForestTicketIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PERMITS_MANAGE))],
) -> ForestTicketOut:
    """ВМҚ 506: one ўрмон чиптаси against an ACTIVE permit (ruling 11).

    409 `ERR-PERM-001` `permit_not_active` when the permit is not `active`;
    422 `ERR-VAL-001` `period_outside_permit` when the requested period
    reaches outside the permit's own; 409 `ERR-PERM-003`
    `active_ticket_exists` when `uq_forest_tickets_active` already holds one
    live ticket for this permit. 403 `ERR-ACL-002` outside the caller's
    leshoz.
    """
    ticket = await service.issue_forest_ticket(db, permit_id, data=payload, actor=actor)
    return ForestTicketOut.model_validate(ticket)


@router.get("/permits/{permit_id}/forest-tickets")
async def list_forest_tickets(
    permit_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PERMITS_MANAGE))],
) -> list[ForestTicketOut]:
    """The permit's whole ВМҚ 506 register, newest first. 404 `ERR-SYS-003`
    for an id that does not exist; 403 `ERR-ACL-002` outside the caller's
    leshoz.
    """
    rows = await service.list_forest_tickets(db, permit_id, actor=actor)
    return [ForestTicketOut.model_validate(row) for row in rows]


# --- Task 8: extend a permit into a new application draft --------------------
#
# `applications.create` — the HOLDER's own gate (migration 0015), the same
# permission `POST /applications` itself carries — never `permits.manage` or
# `permits.issue`: extending is the citizen's act, not a staff one, and the
# service's own `_is_holder` check (run first, `service.extend`'s docstring)
# is what actually decides whose permit this is. A hodim holding neither
# permits permission still cannot reach this route by holding
# `applications.create` alone, because `applications.create` names no
# permit at all.


@router.post("/permits/{permit_id}/extend", status_code=201)
async def extend_permit(
    permit_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_CREATE))],
) -> ApplicationOut:
    """С13: file a new DRAFT `kind='extension'` application against a permit
    still in force — never an edit of the issued document itself
    (`service.extend`'s own docstring: nothing about an issued permit is
    mutable).

    404 `ERR-SYS-003` for an id that does not exist or that this caller is
    not the holder of — the same answer either gets, so the route is not a
    permit-existence oracle. 409 `ERR-PERM-001` `not_extendable` when the
    permit is not `active` or its period has already ended (applied for
    afresh instead, never extended). 409 `ERR-APP-002`
    `extension_already_open` with the existing draft's id when one is
    already open against this permit.
    """
    application = await service.extend(db, permit_id, actor=actor)
    return ApplicationOut.model_validate(application)
