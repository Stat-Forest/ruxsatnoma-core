"""The signed decision — an ERI signature anchored to the STATUS-CHANGE
ATTEMPT, never to the permit (plan `03.11b-permits-lifecycle` ruling 1).

`signatures.uq_signatures_valid_purpose` is unique per `(object_type,
object_id, purpose)`, over VALID rows. Signing a decision over `("permit",
permit.id, "permit_decision")` — the obvious first shape — would let a permit
be suspended exactly once, ever: С13's own example (suspend, then resume, then
suspend again over the permit's life) collides on that very index the second
time around. `decide()` instead mints a fresh `permit_status_history` row,
`history_id = uuid7()`, and signs over `(DECISION_OBJECT_TYPE, history_id,
DECISION_PURPOSE)` — a new object identity per ATTEMPT, so the constraint
never sees two suspensions as the same signable thing.

**`history_id` is neither in the request body nor in the signed bytes.** The
client has to sign bytes it can build before the request exists, which would
mean the caller minting the `permit_status_history` primary key — a
client-controlled primary key on an append-only table is a worse thing to
own than the property it would buy. Instead the SERVER mints `history_id`
between the signer-identity check (`decide()`'s step 6) and `sign()` (step
7), hands it to `sign()` as `object_id` and to `service.set_status()` as
`history_id`, and the anchoring is complete without the client ever knowing
the id exists.

**The order is this file's whole contract** (`decide()`'s own docstring):
lock -> edge -> zone -> ground -> file -> signer identity -> `sign()` ->
`set_status` -> notify -> audit. `sign()` commits the CALLER's entire session
on every refusal (its own transaction contract) — so nothing of `decide()`'s
own may be pending when it is called, and the only writes before it are the
two audited, DELIBERATELY committed denials: the signer-identity refusal this
file writes, and whichever refusal `sign()` itself finds and commits before
raising.

**The consequence, stated rather than hidden.** `decision_document()` is a
pure function of the decision's own facts, none of them per-attempt, so two
identical decisions on the same permit produce identical signed bytes — a
captured envelope is replayable by anyone who already holds `permits.manage`,
the `executor_head` role in this leshoz, AND a live session. Narrow, and it is
the mock adapter's window, not the real one: at 5.2 E-IMZO signing is
challenge-response, and a server-issued nonce is where this would close if it
ever needs to (`decisions.md` #65, not solved here with a two-step API nobody
has asked for).
"""

import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.core.models import MediaFile
from app.db import uuid7
from app.modules.audit import service as audit
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.notifications import service as notifications
from app.modules.permits import grounds, repo, service, signers
from app.modules.permits.models import Permit
from app.modules.permits.schemas import DecisionIn
from app.modules.signatures import service as signatures_service

# "<object>.<verb>", this module's own action (CLAUDE.md's audit invariant) —
# distinct from `service.PERMIT_STATUS_CHANGE`, which `set_status` already
# writes for every mover of `permits.status`. This one records the DECISION
# itself, the same way `service.PERMIT_SIGN` sits beside
# `signatures.SIGNATURE_CREATE` for an ordinary permit signature.
PERMIT_DECIDE = "permit.decide"

# A new `object_type` for `signatures` (never `"permit"` — see the module
# docstring). Signed exactly once per attempt, so there is exactly one
# `DECISION_PURPOSE` and no second line to distinguish it from.
DECISION_OBJECT_TYPE = "permit_decision"
DECISION_PURPOSE = signers.DECISION_PURPOSE


def decision_document(
    *,
    permit: Permit,
    to_status: str,
    reason_code: str,
    legal_basis: str | None,
    doc_file_id: uuid.UUID | None,
) -> bytes:
    """The canonical bytes the head's ERI signs (ruling 3(а)).

    Sorted keys, `ensure_ascii=False`, UTF-8: the same input must produce the
    same bytes on any machine and in any Python version, because the
    signature's stored `doc_hash` is what a later reader checks the statement
    against.

    Everything here is knowable by the CLIENT before it signs — which is why
    the `permit_status_history` id is deliberately absent (module docstring):
    the signature is anchored to that row through `sign()`'s `object_id`, not
    through this statement's content.
    """
    return json.dumps(
        {
            "permit_id": str(permit.id),
            "permit_number": service._permit_number(permit.series, permit.number),
            "to_status": to_status,
            "reason_code": reason_code,
            "legal_basis": (legal_basis or "").strip(),
            "doc_file_id": None if doc_file_id is None else str(doc_file_id),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


async def _assert_decision_doc(
    db: AsyncSession, *, act: str, doc_file_id: uuid.UUID | None
) -> None:
    """The order carrying the ground, checked before the signature (ruling 6).

    Required for `SUSPEND` and `REVOKE` — an authoritative act needs a document
    behind it. `RESUME` needs none: PS-06 «сабаб бартараф этилди» is a fact
    about the WORLD a document cannot add to, so a resumption that supplies
    none is not missing anything.

    `MediaFile` is a core (level 0) model, read directly here — crossing no
    module boundary — exactly as `auth.service._check_poa_file` and
    `payments.backoffice_service._assert_doc_active` already read it for their
    own document checks. `core.files.get_readable` is deliberately NOT used:
    it additionally fetches the bytes from storage and applies a READ-access
    rule (uploader, or any non-applicant role) that has nothing to do with
    what this check is asking, which is only "does a usable PDF exist here".
    """
    if doc_file_id is None:
        if act in (grounds.SUSPEND, grounds.REVOKE):
            raise err("ERR-VAL-001", details={"reason": "doc_file_required"})
        return
    file = await db.get(MediaFile, doc_file_id)
    if file is None or file.status != "active" or file.content_type != "application/pdf":
        raise err("ERR-VAL-001", details={"reason": "doc_file_invalid"})


async def _decision_signer_refusal(db: AsyncSession, permit: Permit, *, actor: User) -> str | None:
    """`None` when `actor` may sign this decision; otherwise WHY not — the same
    shape `service._signer_refusal` uses for the permit's own four lines,
    simplified: a decision has exactly one purpose and no recipient-shaped
    branch to check first.

    Resolves the required role through `signers.decision_role` — **no role
    code appears as a literal at this comparison site** (ruling 4; the lesson
    `083e63d` records why: the map's own declaration is the ONE source, and a
    literal here would be a second one that could silently drift from it).

    Strict equality on `users.organization_id`, deliberately NOT the
    three-axis `Zone` predicate `decide()`'s own step 2 already used: a zone
    answers "whose rows may I see", and a zone-free actor legitimately covers
    the whole republic; this answers "which named official of which named
    organization decides THIS permit's fate", and there is no such thing as a
    republic-wide leshoz head — the same distinction `_signer_refusal`'s own
    docstring draws, including refusing `sys_admin` (whose bypass is about
    privilege, not identity).
    """
    role = signers.decision_role(DECISION_PURPOSE)
    if role is None:
        return "unknown_purpose"
    if await auth_service.role_code(db, actor) != role:
        return "wrong_role"
    if actor.organization_id != permit.organization_id:
        return "wrong_organization"
    return None


async def decide(
    db: AsyncSession,
    permit_id: uuid.UUID,
    *,
    act: str,
    to_status: str,
    data: DecisionIn,
    actor: User,
    event_code: str,
    ip: str | None = None,
) -> Permit:
    """One suspension, resumption or revocation, in the one order that is safe.

    1. lock (`repo.permit_by_id_for_update`) — 404 `ERR-SYS-003` if gone;
    2. `service._assert_organization_in_zone` — 403 `ERR-ACL-002`, the coarse
       territorial gate, checked BEFORE step 3's status check AND step 6's
       finer-grained identity comparison (whole-branch review: this used to
       run AFTER step 3, which made `decide()` a cross-leshoz status oracle —
       an outsider's doomed transition came back 409 naming the permit's REAL
       `from` status instead of a 403 that reveals nothing, the same class of
       leak this stage's own review already closed in `issue_duplicate`): a
       head of a wholly different leshoz meets a determined, information-free
       answer here, never a coin toss between the two 403s and never a peek
       at the permit's current state;
    3. `service._assert_transition` — 409 `ERR-PERM-001`, `from == to` on a
       retry against the status the permit already holds;
    4. `grounds.assert_applicable` — 422 `ERR-VAL-001`, a named reason;
    5. the supporting file (`_assert_decision_doc`) — 422 `ERR-VAL-001`,
       required for suspend and revoke (ruling 6);
    6. the signer's identity (`_decision_signer_refusal`) — audited, then
       COMMITTED, then 403 `ERR-ACL-001` (ruling 4); the one write of our own
       that is genuinely pending when we reach the next step;
    7. `sign()` over `decision_document(...)`, with NOTHING of ours pending —
       every write above this line was already committed, by step 6's own
       denial or not reached at all;
    8. `service.set_status(..., history_id=history_id)` — the one writer of
       `permits.status` this stage adds to;
    9. notify the holder;
    10. audit (`PERMIT_DECIDE`), in the same transaction as the action.
    """
    assert act in grounds.ACTS, f"unknown act: {act}"  # a programming error, not user input

    # 1.
    permit = await repo.permit_by_id_for_update(db, permit_id)
    if permit is None:
        raise err("ERR-SYS-003", details={"permit": str(permit_id)})

    # 2. Before the status check and everything after it — see the docstring
    # above for why the order matters.
    await service._assert_organization_in_zone(db, actor, permit.organization_id)

    # 3.
    service._assert_transition(permit, to_status)

    # 4.
    item = await grounds.assert_applicable(
        db, reason_item_id=data.reason_item_id, act=act, legal_basis=data.legal_basis
    )

    # 5.
    await _assert_decision_doc(db, act=act, doc_file_id=data.doc_file_id)

    # 6.
    refusal = await _decision_signer_refusal(db, permit, actor=actor)
    if refusal is not None:
        await audit.log(
            db,
            action=PERMIT_DECIDE,
            user_id=actor.id,
            object_type=service.OBJECT_TYPE,
            object_id=permit.id,
            result="denied",
            basis="signer_not_authorized",
            new_value={"act": act, "reason": refusal},
            # tz/10 RI-12 «попытка доступа вне территориальных полномочий» —
            # the right role in the wrong leshoz, same treatment `_signer_refusal`
            # already gives the permit's own four lines.
            extra={"risk_indicator": "RI-12"} if refusal == "wrong_organization" else None,
        )
        await db.commit()
        raise err("ERR-ACL-001", details={"reason": "signer_not_authorized"})

    # The row this signature is ABOUT, minted here: after every check that
    # could still refuse, before the one call that cannot be undone. Neither
    # in the request body nor in the signed bytes (module docstring).
    history_id = uuid7()
    document = decision_document(
        permit=permit,
        to_status=to_status,
        reason_code=item.code,
        legal_basis=data.legal_basis,
        doc_file_id=data.doc_file_id,
    )

    # 7. Nothing of ours is pending: every write above this line was either
    # step 6's own deliberate early-commit, or never reached at all.
    await signatures_service.sign(
        db,
        object_type=DECISION_OBJECT_TYPE,
        object_id=history_id,
        purpose=DECISION_PURPOSE,
        document=document,
        pkcs7=data.pkcs7,
        user=actor,
        ip=ip,
    )

    # 8.
    permit = await service.set_status(
        db,
        permit.id,
        to_status=to_status,
        actor=actor,
        reason=data.legal_basis,
        reason_item_id=item.id,
        doc_file_id=data.doc_file_id,
        history_id=history_id,
    )

    # 9. The same recipient rule `_activate`/`_notify_recipient_turn` share.
    await notifications.notify(
        db,
        event_code=event_code,
        recipient_user_id=await service._holder_recipient(db, permit),
        params={"permit_number": service._permit_number(permit.series, permit.number)},
        object_type=service.OBJECT_TYPE,
        object_id=permit.id,
    )

    # 10.
    await audit.log(
        db,
        action=PERMIT_DECIDE,
        user_id=actor.id,
        object_type=service.OBJECT_TYPE,
        object_id=permit.id,
        new_value={"act": act, "status": permit.status, "reason_item_id": str(item.id)},
    )
    return permit
