"""The benefit-verification office's own logic (decisions.md #179) — a
sibling of `service.py`, not an extension of it: `decision.py`'s own shape
("a second file of the same module, not a second module") applied one
narrower, because `service.py` is the one file in this module this track may
not touch (wave-2 track T9's contract, `docs/plans/09-odilxon-demo-fixes.md`).

Everything here answers to `benefits.verify` alone (`permissions.
BENEFITS_VERIFY`) — never to a role name, never to `applications.view_any` or
an ABAC zone. The central office sees every leshoz's rows and ONLY the ones
carrying a certificate-bearing benefit claim (ruling #179: "the whole
country, but only applications with this claim" — a genuinely new predicate,
not a wider zone), a filter `repo.list_certificate_claims`/
`.get_certificate_claim` build directly rather than widening `service.
list_applications`'s own applicant-or-zone scope union.

**Every refusal here is 404 `ERR-SYS-003`**, the same answer `service.
_readable_application` gives a stranger and for the identical reason: an
application carries a citizen's name, plot and herd, and a 403 would make
this office an application-existence oracle for anybody holding the
`benefits.verify` code. An application that exists but carries no
certificate-bearing claim is refused exactly like one that does not exist at
all — `repo.get_certificate_claim`/`.get_certificate_claim_for_update` return
`None` for both, so there is only one branch here to get wrong, not two
(ruling #179 task 3's own negative test: a verifier reading an unrelated
application gets what a stranger gets).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.modules.applications import repo
from app.modules.applications.models import Application, ApplicationDocument
from app.modules.audit import service as audit
from app.modules.auth.models import User

# "<object>.<verb>", ruling 17's own convention (`service.py`'s docstring) —
# `benefit_claim`, not `application`, because what moves is the claim's OWN
# state machine (`models.BENEFIT_VERIFICATION_STATUSES`), a column distinct
# from `applications.status`, which these actions never touch.
BENEFIT_CLAIM_VERIFY = "benefit_claim.verify"
BENEFIT_CLAIM_REJECT = "benefit_claim.reject"


async def list_claims(
    db: AsyncSession,
    *,
    verification_status: str | None,
    offset: int,
    limit: int,
) -> tuple[list[Application], int]:
    """The office's queue — every certificate-bearing claim, country-wide.

    No actor parameter: the router's `require_permission(BENEFITS_VERIFY)` is
    the whole gate, and there is no second, narrower question to ask of WHO is
    asking — ruling #179 is explicit that this is one shared queue for
    "several users", not a per-verifier assignment.
    """
    return await repo.list_certificate_claims(
        db, verification_status=verification_status, offset=offset, limit=limit
    )


async def get_claim(db: AsyncSession, application_id: uuid.UUID) -> Application:
    """One claim, or the stranger's 404 (module docstring)."""
    application = await repo.get_certificate_claim(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    return application


async def get_claim_detail(
    db: AsyncSession, application_id: uuid.UUID
) -> tuple[Application, list[ApplicationDocument]]:
    """`get_claim` plus its documents — the certificate's supporting file,
    attached through the ordinary document mechanism (`repo.list_documents`,
    unchanged from the applicant-facing module). Two queries, never merged
    into one: `list_certificate_claims` (the queue) stays document-free on
    purpose, so paging it costs one query, not one plus N."""
    application = await get_claim(db, application_id)
    documents = await repo.list_documents(db, application.id)
    return application, documents


async def _transition(
    db: AsyncSession,
    application_id: uuid.UUID,
    *,
    to_status: str,
    action: str,
    actor: User,
    rejection_reason: str | None,
) -> Application:
    """The one body `verify_claim`/`reject_claim` share: lock the row (`repo.
    get_certificate_claim_for_update`, the same reasoning `service.
    get_application_for_update`'s own docstring states — two verifiers
    deciding the same claim at once serialise on it rather than racing),
    refuse anything but `pending -> to_status`, write and audit.

    409 `ERR-APP-004` (`reason="not_pending"`) covers every OTHER value this
    row could hold at this point: `verified`/`rejected` already decided
    (no route reverses one), or a fresh RE-submission's `pending` racing this
    very call — never `not_required`/nonexistent, which the row lock above
    already turned into the 404 a stranger gets.
    """
    application = await repo.get_certificate_claim_for_update(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    if application.benefit_verification_status != "pending":
        raise err(
            "ERR-APP-004",
            details={"reason": "not_pending", "status": application.benefit_verification_status},
        )
    now = datetime.now(UTC)
    await repo.set_benefit_verification(
        db,
        application,
        status=to_status,
        verified_by=actor.id,
        verified_at=now,
        rejection_reason=rejection_reason,
    )
    await audit.log(
        db,
        action=action,
        user_id=actor.id,
        object_type="application",
        object_id=application.id,
        new_value={
            "benefit_verification_status": to_status,
            **({"reason": rejection_reason} if rejection_reason is not None else {}),
        },
    )
    return application


async def verify_claim(db: AsyncSession, application_id: uuid.UUID, *, actor: User) -> Application:
    """`POST /applications/benefit-verifications/{id}/verify` — the claim's
    only positive transition, `pending -> verified`. T6's issuance guard
    (ruling #179's route point, not this track's) reads `verified` as one of
    the two statuses that let a permit print."""
    return await _transition(
        db,
        application_id,
        to_status="verified",
        action=BENEFIT_CLAIM_VERIFY,
        actor=actor,
        rejection_reason=None,
    )


async def reject_claim(
    db: AsyncSession, application_id: uuid.UUID, *, actor: User, reason: str
) -> Application:
    """`POST /applications/benefit-verifications/{id}/reject` — `pending ->
    rejected`, with the reason ruling #179 makes MANDATORY (`schemas.
    BenefitClaimRejectIn.reason`, `min_length=1` — checked at the wire AND
    here, the same belt `ApplicationRejectIn`'s own grounds wear). Written
    into `benefit_rejection_reason`, which T6's issuance guard names in its
    own refusal."""
    return await _transition(
        db,
        application_id,
        to_status="rejected",
        action=BENEFIT_CLAIM_REJECT,
        actor=actor,
        rejection_reason=reason,
    )
