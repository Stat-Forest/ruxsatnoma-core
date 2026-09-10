"""The benefit-verification logic (decisions.md #179, moved to the leshoz by
#182) — a sibling of `service.py`, the same "second file of the same module,
not a second module" shape `decision.py` already uses.

**Ruling #182 moved this office from a central, country-wide role to the
leshoz's own review.** `benefits.verify` now sits on `executor_staff`/
`executor_head` (migration `0053`), and every route below answers to that
code PLUS the application's own read/zone rule — `flow._readable_application`
for the read, `flow._assert_in_actor_zone` for the write, both the SAME
functions `GET /applications/{id}` and `decision.approve`/`.reject` already
use. A leshoz reviewer who could not otherwise read an application cannot
verify its claim either; a stranger, or a reviewer of ANOTHER leshoz, is told
the same 404 `ERR-SYS-003` a nonexistent id gets — an application carries a
citizen's name, plot and herd, and a 403 would make this route an
application-existence oracle. The country-wide list route this office used to
carry (`GET /applications/benefit-verifications`) and `repo`'s own
claim-visibility predicate built for it are GONE: a leshoz reviewer works this
claim from the application card it already reads, not a queue of its own.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.modules.applications import repo
from app.modules.applications import service as flow
from app.modules.applications.models import Application, ApplicationDocument
from app.modules.audit import service as audit
from app.modules.auth.models import User

# "<object>.<verb>", ruling 17's own convention (`service.py`'s docstring) —
# `benefit_claim`, not `application`, because what moves is the claim's OWN
# state machine (`models.BENEFIT_VERIFICATION_STATUSES`), a column distinct
# from `applications.status`, which these actions never touch.
BENEFIT_CLAIM_VERIFY = "benefit_claim.verify"
BENEFIT_CLAIM_REJECT = "benefit_claim.reject"


async def get_claim(db: AsyncSession, application_id: uuid.UUID, *, actor: User) -> Application:
    """One claim the CALLER may read, or the same 404 a stranger or an id
    that never existed gets (module docstring).

    `flow._readable_application` is the whole ownership/zone rule; on top of
    it, an application carrying no certificate-bearing claim
    (`repo.CERTIFICATE_BEARING_STATUSES`) reads exactly like one that does
    not exist — this office has nothing to show for it either way, so there
    is only one branch here to get wrong, not two.
    """
    application = await flow._readable_application(db, application_id, actor=actor)
    if application.benefit_verification_status not in repo.CERTIFICATE_BEARING_STATUSES:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    return application


async def get_claim_detail(
    db: AsyncSession, application_id: uuid.UUID, *, actor: User
) -> tuple[Application, list[ApplicationDocument]]:
    """`get_claim` plus its documents — the certificate's supporting file,
    attached through the ordinary document mechanism (`repo.list_documents`,
    unchanged from the applicant-facing module). Two queries, never merged
    into one: a detail read is the one-off, not the queue this office used to
    page (ruling #182 retired it), so there is nothing left to optimise for
    N+1 here — kept separate anyway, matching `service.get_card`'s own shape."""
    application = await get_claim(db, application_id, actor=actor)
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
    """The one body `verify_claim`/`reject_claim` share: lock the row
    (`repo.get_application_for_update`, `decision._decidable`'s own shape —
    two reviewers deciding the same claim at once serialise on it rather
    than racing), the SAME territorial rule `decision.approve`/`.reject`
    apply (`flow._assert_in_actor_zone` — RI-12 recorded before it raises,
    decision #40 ruling 2), the application's own status (`IN_REVIEW` only —
    ruling #182: "the moderator checks it when the application comes in";
    409 `ERR-APP-004` `not_in_review`), then refuse anything but
    `pending -> to_status`.

    409 `ERR-APP-004` (`reason="not_pending"`) covers every OTHER value the
    CLAIM itself could hold at this point: `not_required` (nothing to
    verify — the same 404 `get_claim` gives a reader would be nicer, but this
    write path is reached only by an actor who already holds `benefits.
    verify` AND is in zone, so a 409 naming the honest status costs nothing
    and needs no second lookup), `verified`/`rejected` already decided (no
    route reverses one), or a fresh RE-submission's `pending` racing this
    very call.
    """
    application = await repo.get_application_for_update(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    await flow._assert_in_actor_zone(db, application, actor=actor, action=action)
    if application.status != flow.IN_REVIEW_STATUS:
        raise err(
            "ERR-APP-004",
            details={"reason": "not_in_review", "status": application.status},
        )
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
    only positive transition, `pending -> verified`. `decision._assert_
    benefit_decided` (ruling #182's other line) reads this same status
    before `approve` may move the application anywhere."""
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
    into `benefit_rejection_reason`, which `decision.reject` reads as the
    APPLICATION's own grounds when the head gives none of their own
    (ruling #182)."""
    return await _transition(
        db,
        application_id,
        to_status="rejected",
        action=BENEFIT_CLAIM_REJECT,
        actor=actor,
        rejection_reason=reason,
    )
