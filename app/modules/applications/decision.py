"""The head's decision — approve, reject, and the over-limit forward
(plan 03.9a task 7; design/03 § Заявки).

**A second file of the same module, not a second module.** `service.py` was
already ~1900 lines against the plan's 800-line split trigger when this task
started, and the plan names the decision path as the split target. Everything
here is `applications`' own code and reaches into `service.py` for the helpers
task 6 built for exactly this (`_assert_in_actor_zone`, `_apply_transition`,
`_claim_assignment`, `_package_bytes` via `package`) rather than copying them —
the private-name imports below are intra-module, and no other module may follow
them.

Three vocabularies, one moment, and none of them interchangeable
(`applications/events.py` carries the table):

    audit_log.action              "application.approve"   present-tense verb
    notification_templates        "application.approved"  past participle, DOTTED
    core.events bus name          "application_approved"  flat, no dot

**Ruling 9а, the whole of it.** An application whose amount or area is beyond
the deciding role's ceiling (decision #29) is neither approved nor refused: it
is FORWARDED to the parent organization and its status does not move. The
alternative — approve it and flag it — cannot work here, because 3.10a
subscribes to `application_approved` and would invoice a decision nobody made.

**Ruling R5, the signed bytes.** A decision is signed over EXACTLY the bytes
`GET /applications/{id}/package` serves, because that is the only document the
client can have seen: a detached PKCS#7 cannot be produced over bytes that were
never fetched, and 3.9a exposes no decision-specific document. The rejection's
`reason_item_id` and `legal_basis` are therefore recorded and audited but are
NOT inside the signed bytes — see the comment at the `sign()` call, and 3.9b's
open question.

**Ordering, and why `sign()` comes where it does.** `signatures.service.sign()`
COMMITS the caller's session on every refusal path, taking everything pending
with it (its own TRANSACTION CONTRACT, plan ruling 19). So every check that can
refuse — the zone, the transition, the role limit, the rejection grounds — runs
BEFORE it, and nothing is written before it either: a refused ERI must not leave
a half-decided application committed, and a request that cannot succeed must
never spend a signature.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.core.events import Event, publish
from app.modules.admin import repo as admin_repo
from app.modules.admin import service as admin_service
from app.modules.admin.models import ClassifierItem
from app.modules.applications import repo
from app.modules.applications import service as flow
from app.modules.applications.events import APPLICATION_APPROVED, APPLICATION_REJECTED
from app.modules.applications.models import Application, ApplicationStatusHistory
from app.modules.audit import service as audit
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.notifications import service as notifications_service
from app.modules.signatures import service as signatures_service

# Ruling 17, beside `service.APPLICATION_SUBMIT`/`.APPLICATION_START_REVIEW`: a
# flow verb audits under its OWN name, so the journal can say whether an
# application moved because it was approved, refused, or bounced up the ladder.
# `application.forward` moves no status at all and is audited all the same —
# an escalation is a decision about who decides, and `tz/10`'s reviewer has to
# be able to see it.
APPLICATION_APPROVE = "application.approve"
APPLICATION_REJECT = "application.reject"
APPLICATION_FORWARD = "application.forward"

# `notification_templates.event_code` — DOTTED, seeded by migration 0009, and a
# DIFFERENT vocabulary from the bus names imported above. Passing
# `APPLICATION_APPROVED` ("application_approved") where an `event_code` belongs
# finds no template, and `notify()` answers that by writing a raw fallback
# string in-app and sending NOTHING by SMS or e-mail, silently, on every
# decision this system ever makes (controller ruling R6).
NOTIFY_APPLICATION_APPROVED = "application.approved"
NOTIFY_APPLICATION_REJECTED = "application.rejected"

APPROVED_STATUS = "APPROVED"
REJECTED_STATUS = "REJECTED"

# The classifier a rejection's `reason_item_id` must belong to — RJ-01…RJ-15,
# seeded by migration 0005 out of `tz/10` § 8.2. Membership is checked, not mere
# existence: `classifier_items` holds every classifier's values in one table, so
# an id-only check would accept a benefit category or a document type as a legal
# ground for refusing a citizen (lesson: an existence check is not a validity
# check).
REJECTION_CLASSIFIER_CODE = "rejection_reasons"

# The language every `classifier_items.name` is guaranteed to carry (design/02
# principle 3) — the fallback when the recipient's own language has no
# translation of the reason.
FALLBACK_LANGUAGE = "uz_cyrl"

# What a forward writes into the bounce row's `reason_text` (controller minor 4).
# A STABLE TOKEN, never a sentence: `reason_text` surfaces on the
# citizen-visible timeline, and this project keeps user-facing wording in
# versioned `notification_templates` rows an admin owns, never in code — an
# English sentence in an Uzbek/Russian government UI is a defect the front end
# cannot fix. WHICH ceilings were exceeded, and by how much, live in the audit
# entry's `new_value`, which is where that detail belongs. 3.9b's return and
# request-info reasons inherit the same convention.
FORWARD_REASON = "role_limit_exceeded"


def _over_limit(
    *,
    amount: Decimal | None,
    area: Decimal | None,
    max_amount: Decimal | None,
    max_area: Decimal | None,
) -> list[str]:
    """Which of decision #29's two ceilings this application is beyond — `[]`
    when it is within both, and therefore decidable here.

    **A NULL ceiling means NO ceiling** (decision #29), never zero: a limit
    implemented as `max_amount or Decimal(0)` would forward every application
    ever filed, and all eleven roles ship with both columns NULL (migration
    0003), so that mistake would be total rather than rare.

    The two axes are independent — a role may cap money and not area, or the
    reverse — so this returns the LIST of the ones that fired rather than a
    bool: the escalation's history row says which, and a reviewer at the parent
    organization has to be able to read it.

    `amount`/`area` reach here as `None` only in a case `_limits` has already
    refused; the `is not None` guards below are the second half of that pair and
    keep this function honest when read on its own, rather than a place where a
    missing value quietly passes as "within the limit".
    """
    over: list[str] = []
    if max_amount is not None and amount is not None and amount > max_amount:
        over.append("amount")
    if max_area is not None and area is not None and area > max_area:
        over.append("area")
    return over


async def _decidable(
    db: AsyncSession, application_id: uuid.UUID, *, to_status: str, actor: User, action: str
) -> Application:
    """The LOCKED application this actor may decide, or the refusal — the
    preamble `approve` and `reject` share, so a precondition belonging to the
    whole transition lives in one function both call (lesson).

    Locked from the start (`repo.get_application_for_update`): this is a
    read-check-write over `status`, and the head's own «одобрить» double-click,
    or an approve racing a `cancel`, would otherwise both pass
    `_assert_transition` and both write.

    The order of the refusals is what the tests pin: an out-of-zone caller is
    told 404 before anything about the status is revealed, and only then is a
    non-IN_REVIEW application a 409 `ERR-APP-004`. Reversing them would make
    these routes an application-existence oracle for anybody holding a session.

    `_assert_in_actor_zone` COMMITS its RI-12 trail before raising (decision #40
    ruling 2), which is safe here precisely because nothing has been written
    yet.
    """
    application = await repo.get_application_for_update(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    await flow._assert_in_actor_zone(db, application, actor=actor, action=action)
    flow._assert_transition(application, to_status)
    return application


async def _limits(
    db: AsyncSession, application: Application, *, actor: User
) -> tuple[Decimal | None, Decimal | None, list[str]]:
    """Decision #29's inputs and its verdict: `(amount, area, over)`.

    The AMOUNT is the application's stored calculation — `service.
    current_calculation`, the single frozen price 3.7's preview/save split
    exists to protect — never a fresh `preview`, which would price the decision
    at a different moment from the invoice 3.10a raises off the same row.

    The AREA is `applications.requested_area_ha`, frozen at submission from the
    contour version's own `area_ha` (ruling 22). It is nullable, and a NULL
    compared against a real `max_approve_area` silently disables half of
    decision #29 — so a missing one is refused here rather than skipped.

    A missing CALCULATION is refused for two reasons at once: the amount half
    of the limit could not be evaluated, and `payments.service.issue_invoice` —
    which runs inside this very transaction the moment the approval publishes —
    refuses the same condition with the same code anyway. Refusing here means
    the ERI is never spent on it.
    """
    role = await auth_service.role_of(db, actor)
    if role is None:  # FK-guaranteed; never fall through to "no limits"
        raise err("ERR-SYS-001", details={"reason": "actor_role_missing"})

    calculation = await flow.current_calculation(db, application.id)
    if calculation is None:
        raise err(
            "ERR-VAL-001",
            details={"reason": "no_calculation", "application": str(application.id)},
        )
    amount = calculation.amount
    area = application.requested_area_ha
    if role.max_approve_area is not None and area is None:
        raise err(
            "ERR-VAL-001",
            details={"reason": "requested_area_unknown", "application": str(application.id)},
        )
    return (
        amount,
        area,
        _over_limit(
            amount=amount,
            area=area,
            max_amount=role.max_approve_amount,
            max_area=role.max_approve_area,
        ),
    )


async def _forward(
    db: AsyncSession,
    application: Application,
    *,
    actor: User,
    amount: Decimal | None,
    area: Decimal | None,
    over: list[str],
) -> uuid.UUID:
    """Ruling 9а: escalate to the parent organization and change NOTHING about
    the status. Returns the organization the application was forwarded TO.

    **No parent is a loud 422**, never a silent approval and never a silent
    stall: at the agency there is nowhere to escalate to, and an unresolvable
    escalation that answered 200 would leave the application parked forever with
    nobody able to decide it.

    The assignment is written through `service._claim_assignment`, which
    supersedes the active row: `uq_application_assignments_active` is UNIQUE on
    `(application_id) WHERE is_active`, so a blind insert beside the reviewer's
    own row is an `IntegrityError`.

    `assigned_org_id` MOVES with the assignment, and `assigned_user_id` is
    cleared. That is what the escalation means — the zone rule
    (`service._effective_organization`) reads that column, so a head at the
    parent organization can only see and decide the application once it points
    at them. The cost is real and deliberate: the forwarding head, if zoned to
    the leshoz, no longer sees the application they escalated.

    **AN APPLICATION NOBODY HAS TAKEN INTO WORK AT ITS CURRENT LEVEL CANNOT BE
    ESCALATED FROM IT** — the guard below, and the one that stops a head walking
    the whole ladder alone. The zone does NOT stop them:
    `service._assert_in_actor_zone` returns immediately for an actor whose
    `Zone` is empty on all three axes, so an agency- or republic-level head is
    unrestricted nationwide, and a second `POST /approve` would re-read
    `_effective_organization` (now the parent), escalate to the GRANDPARENT,
    write a third assignment row and a second bogus bounce entry — one level per
    click, until the agency answers 422, with the leshoz that filed it no longer
    able to see it.

    `assigned_user_id` is the honest test for "somebody here is working on
    this": `start_review` sets it to the reviewer who took the application into
    work, and this function clears it, so it is non-null exactly once per level
    and only after a human at that level has claimed the file. A LEGITIMATE
    second escalation therefore needs somebody at the parent organization to
    claim it first — which in 3.9a nothing can do (`start-review` requires
    SUBMITTED, and `applications.assign` is 3.9b's route). So a two-level
    escalation is refused with a 409 naming the fact, rather than performed
    silently by whoever clicked twice; 3.9b, which owns assignment, is what
    makes it reachable.
    """
    if application.assigned_user_id is None:
        raise err(
            "ERR-APP-004",
            details={
                "reason": "not_claimed_at_this_level",
                "assigned_org_id": None
                if application.assigned_org_id is None
                else str(application.assigned_org_id),
            },
        )
    organization_id = await flow._effective_organization(db, application)
    if organization_id is None:
        # `application_assignments.org_id` is NOT NULL and there is nothing to
        # put in it. Unreachable from IN_REVIEW — `start_review` refuses the
        # same condition before it can get there — but a 409 naming the fact
        # beats an IntegrityError/500 the day another path arrives here.
        raise err(
            "ERR-APP-004",
            details={"reason": "no_organization", "status": application.status},
        )
    parent = await admin_service.parent_organization(db, organization_id)
    if parent is None:
        raise err(
            "ERR-VAL-001",
            details={
                "reason": "no_parent_organization",
                "organization": str(organization_id),
                "over_limit": over,
            },
        )

    await flow._claim_assignment(
        db,
        application,
        org_id=parent.id,
        user_id=None,
        reason=flow.ASSIGNMENT_MANUAL,
        actor=actor,
    )
    application.assigned_org_id = parent.id
    application.assigned_user_id = None

    # **NOT a transition, and deliberately not written through
    # `_apply_transition`** (controller ruling R22). The application's status
    # genuinely has not changed — that is the whole of ruling 9а — so
    # `from_status == to_status`, which `_assert_transition` correctly refuses
    # as a self-loop `APPLICATION_TRANSITIONS` does not contain. The row is
    # still owed: the bounce has to be visible on the timeline WITH its reason,
    # and `application_assignments.reason` is CHECK-constrained to
    # auto/absence/manual and carries no free text, so `reason_text` here is the
    # only place the reason can live — as `FORWARD_REASON`, a stable token, for
    # the reason that constant states. A reader must not mistake this row for a
    # transition; the equal statuses are the tell.
    await repo.add_status_history(
        db,
        ApplicationStatusHistory(
            application_id=application.id,
            from_status=application.status,
            to_status=application.status,
            changed_by=actor.id,
            reason_text=FORWARD_REASON,
        ),
    )
    await db.refresh(application)
    await audit.log(
        db,
        action=APPLICATION_FORWARD,
        user_id=actor.id,
        object_type="application",
        object_id=application.id,
        old_value={"assigned_org_id": str(organization_id)},
        new_value={
            "assigned_org_id": str(parent.id),
            "status": application.status,
            # WHICH ceilings fired and the numbers that fired them — an
            # escalation is only auditable if the journal says why this
            # application and not the next one. `_json_safe` because
            # `audit_log`'s JSONB goes through the stock `json.dumps`, which
            # cannot encode a `Decimal` (lesson).
            "over_limit": over,
            "amount": flow._json_safe(amount),
            "requested_area_ha": flow._json_safe(area),
        },
        basis=FORWARD_REASON,
    )
    return parent.id


async def _sign_decision(
    db: AsyncSession, application: Application, *, pkcs7: str, actor: User
) -> None:
    """The head's ERI over the decision (ruling 25: one decision object per
    application, however many times it was submitted).

    **The signed bytes are EXACTLY what `GET /applications/{id}/package`
    serves** (controller ruling R5), fetched through `service.package` rather
    than rebuilt here so the two can never drift. What that does and does not
    attest to, stated where it is easy to get wrong: the signature covers the
    APPLICATION — applicant, activity, contour version, period, herd, price and
    rule version — and NOT the decision's own words. A rejection's
    `reason_item_id` and `legal_basis` are outside these bytes in 3.9a; they are
    recorded on the history row and in the audit journal, and nobody may read
    this signature as attesting to the refusal text. Filed as an open question
    for 3.9b, which owns the review flow.

    Ruling 23 applies here as it does at submission: the package is priced
    afresh at `business_today()`, so a tariff, a rule parameter, a norm or
    midnight in Tashkent moving between the head's `GET /package` and this
    POST changes the bytes. **Ruling 18 (в), 2026-09-05**, the same fix
    `submit` carries: `content_changed_reason=flow.STALE_PACKAGE_REASON`
    below means a head whose decision-signature is valid over a package that
    moved out from under them meets `details.reason == "package_changed"`,
    not the bare `"signature_invalid"` a forged or corrupted ERI still gets.

    `sign()` is the first thing on either decision path that can commit, which
    is why every refusal runs ahead of it and nothing is written before it.
    """
    document = await flow.package(db, application.id, actor=actor)
    await signatures_service.sign(
        db,
        object_type=flow.DECISION_OBJECT_TYPE,
        object_id=application.id,
        purpose=flow.DECISION_PURPOSE,
        document=document,
        pkcs7=pkcs7,
        user=actor,
        content_changed_reason=flow.STALE_PACKAGE_REASON,
    )


async def _reason_item(db: AsyncSession, reason_item_id: uuid.UUID) -> ClassifierItem:
    """The rejection ground, or 422. An ACTIVE item of the `rejection_reasons`
    classifier and of no other — membership, not mere existence (lesson).

    Read through `admin.repo`, never a query of `classifier_items` from this
    module (CLAUDE.md: reference data goes through `admin`). Same shape as
    `service._assert_doc_type`, which guards the same table for document types.
    """
    item = await admin_repo.get_classifier_item(db, reason_item_id)
    classifier = await admin_repo.get_classifier_by_code(db, REJECTION_CLASSIFIER_CODE)
    if (
        item is None
        or classifier is None
        or item.classifier_id != classifier.id
        or item.status != "active"
    ):
        raise err("ERR-VAL-001", details={"reason": "unknown_rejection_reason"})
    return item


async def _notify_decision(
    db: AsyncSession, application: Application, *, event_code: str, params: dict[str, Any]
) -> None:
    """One notification per decision, in THIS transaction (3.5's rule), to the
    individual applicant's own account when there is one and otherwise to
    whoever filed — `service._notification_recipient` owns that definition."""
    await notifications_service.notify(
        db,
        event_code=event_code,
        recipient_user_id=await flow._notification_recipient(db, application),
        params=params,
        object_type="application",
        object_id=application.id,
    )


async def _recipient_language(db: AsyncSession, application: Application) -> str:
    """The language the decision notice will be rendered in, resolved BEFORE
    `notify` so a parameter carrying admin-authored multilingual text (a
    rejection reason's own `name`) can be picked in the same language as the
    template around it. `notify` resolves the identical contact again a moment
    later; `db.get`'s identity map makes the repeat free."""
    contact = await auth_service.get_notification_contact(
        db, await flow._notification_recipient(db, application)
    )
    return contact.language if contact is not None else FALLBACK_LANGUAGE


async def approve(
    db: AsyncSession, application_id: uuid.UUID, *, pkcs7: str, actor: User
) -> tuple[Application, uuid.UUID | None]:
    """`POST /applications/{id}/approve` — the head signs, or the application
    goes up the ladder. Returns `(application, forwarded_to_organization)`, the
    second being `None` on a real approval.

    **No caller ever observes APPROVED.** 3.10a's
    `payments.subscribers.on_application_approved` is registered in
    `app/event_subscriptions.py` and runs synchronously INSIDE this
    transaction (ruling 3а), so the invoice is issued and the application is
    already INVOICED by the time this returns. APPROVED exists in
    `application_status_history` and nowhere else.

    409 `ERR-APP-004` in any status but IN_REVIEW; 404 `ERR-SYS-003` outside the
    caller's zone (RI-12 recorded first); 422 `ERR-VAL-001` when the application
    has no stored calculation, when `requested_area_ha` is unknown while the
    role caps area, and when an over-limit application has no parent
    organization to escalate to; 422 `ERR-SIGN-001` for an invalid ERI.
    """
    application = await _decidable(
        db, application_id, to_status=APPROVED_STATUS, actor=actor, action=APPLICATION_APPROVE
    )
    amount, area, over = await _limits(db, application, actor=actor)
    if over:
        # Ruling 9а: nothing is signed and the status does not change, because
        # the application genuinely has not been decided.
        return application, await _forward(
            db, application, actor=actor, amount=amount, area=area, over=over
        )

    await _sign_decision(db, application, pkcs7=pkcs7, actor=actor)
    # `decided_at` belongs to the flow verb that owns the decision, never to
    # `set_status`, which moves `status` and nothing else. Set BEFORE the
    # transition so that ONE update carries it: `updated_at` is
    # `onupdate=func.now()`, which SQLAlchemy leaves EXPIRED after a plain
    # UPDATE (lesson: the row in memory is not what Postgres stored), and
    # `_apply_transition`'s own `db.refresh` repopulates it. A column written
    # AFTER that refresh triggers a second UPDATE, re-expires `updated_at`, and
    # the router's serialization then lazy-loads it outside the async context —
    # `MissingGreenlet`, i.e. a 500 on a decision that actually succeeded.
    application.decided_at = datetime.now(UTC)
    await flow._apply_transition(
        db,
        application,
        to_status=APPROVED_STATUS,
        action=APPLICATION_APPROVE,
        actor=actor,
    )
    await _notify_decision(
        db,
        application,
        event_code=NOTIFY_APPLICATION_APPROVED,
        params={"application_number": application.number},
    )
    # `application_id` and NOTHING else — the payload contract frozen in
    # `applications/events.py`. An amount here would be a second source of truth
    # for money beside the stored calculation, which is exactly what 3.10a's
    # handler reads through `service.current_calculation` instead.
    await publish(db, Event(name=APPLICATION_APPROVED, payload={"application_id": application.id}))
    return application, None


async def reject(
    db: AsyncSession,
    application_id: uuid.UUID,
    *,
    pkcs7: str,
    reason_item_id: uuid.UUID,
    legal_basis: str,
    actor: User,
) -> Application:
    """`POST /applications/{id}/reject` — IN_REVIEW -> REJECTED, with grounds.

    **`tz/04` С8: a refusal by the state carries an RJ-* reason AND a legal
    basis**, and both are required by `ApplicationRejectIn` rather than
    validated here, so a body missing either is 422 `ERR-VAL-001` before this
    function — and therefore before `sign()` — is ever reached. The one thing
    the schema cannot check is that the id names an ACTIVE
    `rejection_reasons` item, and that runs here, still ahead of the signature.

    The grounds land in three places, each answering a different question: the
    `application_status_history` row (what the timeline shows), the
    `applications` row itself (`rejection_reason_item_id` / `decision_basis` —
    what the card and every later reader see without walking the history), and
    the audit journal.

    No role limit applies. Decision #29 caps what a head may GRANT — an
    escalation exists because approving beyond one's ceiling commits the state
    to something; refusing commits it to nothing, and a refusal a citizen can
    appeal is not made better by bouncing it upward first.
    """
    application = await _decidable(
        db, application_id, to_status=REJECTED_STATUS, actor=actor, action=APPLICATION_REJECT
    )
    item = await _reason_item(db, reason_item_id)

    await _sign_decision(db, application, pkcs7=pkcs7, actor=actor)
    # Set BEFORE the transition, so ONE update carries the whole decision — see
    # `approve`'s note on `updated_at` for why a column written after
    # `_apply_transition`'s `db.refresh` turns a successful rejection into a 500.
    application.rejection_reason_item_id = reason_item_id
    application.decision_basis = legal_basis
    application.decided_at = datetime.now(UTC)
    await flow._apply_transition(
        db,
        application,
        to_status=REJECTED_STATUS,
        action=APPLICATION_REJECT,
        actor=actor,
        reason_item_id=reason_item_id,
        legal_basis=legal_basis,
    )

    language = await _recipient_language(db, application)
    await _notify_decision(
        db,
        application,
        event_code=NOTIFY_APPLICATION_REJECTED,
        params={
            "application_number": application.number,
            # The reason as the citizen reads it. `classifier_items.name` is
            # multilingual and `uz_cyrl` is the one key design/02 guarantees, so
            # it is the fallback — never `item.code`, which would render as
            # «Причина: RJ-03».
            "reason": item.name.get(language) or item.name[FALLBACK_LANGUAGE],
        },
    )
    await publish(db, Event(name=APPLICATION_REJECTED, payload={"application_id": application.id}))
    return application
