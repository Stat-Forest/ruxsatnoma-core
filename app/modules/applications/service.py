"""Applications service — business logic over the `applications` tables
(design/02 § applications; plan `03.9a-applications-core`).

Branch 1 (`stage-3.9a-core`) ships exactly three functions of plan Task 8's
public surface — `get`, `current_calculation`, `set_status` — on top of
`core/numbers.py` (moved forward out of Task 5) and the event bus shipped in
the two commits before this one. Branch 2 adds the rest: `precheck`,
`submit`, the duplicate guard, and the full decision flow (`start_review`,
`approve`, `reject`, `return_to_applicant`, `cancel`, `forward`). See the
"Task 8 public surface" comment below for the contract this file promises
levels 4+ (payments 3.10, permits 3.11) today."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.modules.applications import repo
from app.modules.applications.models import Application, ApplicationStatusHistory
from app.modules.audit import service as audit
from app.modules.auth.models import User
from app.modules.norms import service as norms_service
from app.modules.norms.models import Calculation

# Ruling 17 (decision #38): audit action codes are "<object>.<verb>" in
# English, and the constant lives with the acting module — audit (level 0)
# knows no domain vocabulary. `set_status` is the ONLY writer in this branch,
# so it is the only constant declared so far; branch 2 adds
# APPLICATION_SUBMIT/.start_review/.approve/.reject/.cancel/.forward
# alongside it for the flow verbs that route around `set_status` entirely
# (see `set_status`'s own docstring for why).
APPLICATION_STATUS_CHANGE = "application.status_change"

# tz/05's transition table, verbatim (plan 03.9a task 8, the brief's own
# copy). All fourteen `APPLICATION_STATUSES` are keys; `ARCHIVED` is
# terminal. No status maps to itself — tz/05 has no self-loop anywhere in
# this table, so a transition to the status an application already holds is
# exactly as illegal as any other jump not listed here (3.10's retry paths
# will attempt this; see `test_public_surface.py`).
APPLICATION_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"SUBMITTED", "CANCELLED"}),
    "SUBMITTED": frozenset({"IN_REVIEW", "RETURNED", "REJECTED"}),
    "IN_REVIEW": frozenset({"PENDING_INFO", "APPROVED", "REJECTED", "RETURNED"}),
    "PENDING_INFO": frozenset({"IN_REVIEW", "CANCELLED"}),
    "RETURNED": frozenset({"SUBMITTED", "CANCELLED"}),
    "APPROVED": frozenset({"INVOICED"}),
    "INVOICED": frozenset({"PAID", "EXPIRED_UNPAID", "CANCELLED"}),
    "PAID": frozenset({"PERMIT_ISSUED"}),
    "PERMIT_ISSUED": frozenset({"CLOSED"}),
    "REJECTED": frozenset({"ARCHIVED"}),
    "CANCELLED": frozenset({"ARCHIVED"}),
    "EXPIRED_UNPAID": frozenset({"ARCHIVED"}),
    "CLOSED": frozenset({"ARCHIVED"}),
    "ARCHIVED": frozenset(),
}
# One-source-of-truth check: the keys and every target above must be members
# of APPLICATION_STATUSES (models.py) — see
# test_public_surface.py::test_transition_table_has_all_fourteen_statuses_and_only_real_targets.


def _assert_transition(application: Application, to_status: str) -> None:
    """A transition not listed in `APPLICATION_TRANSITIONS[application.status]`
    is a conflict with the application's CURRENT state, not a malformed
    request body — `ERR-APP-004` (409), never `ERR-VAL-001`. Mirrors
    `gis.service._assert_transition`'s identical shape over `TRANSITIONS`."""
    if to_status not in APPLICATION_TRANSITIONS[application.status]:
        raise err(
            "ERR-APP-004",
            details={"reason": "bad_transition", "from": application.status, "to": to_status},
        )


# --- Task 8: the public surface for levels 4+ (payments 3.10, permits 3.11) -
#
# Branch 1 ships three of Task 8's functions below — `get`,
# `current_calculation`, `set_status` — plus the four event names already
# shipped in `applications/events.py`. Branch 2 adds `precheck`, `submit` and
# the decision flow; everything below still applies to the FULL surface,
# stated now because 3.10a and 3.11 are blocked on it today, not when branch
# 2 lands.
#
# - `get(db, application_id) -> Application | None` — no permission or zone
#   rule; the caller is another service inside this process, mirroring
#   `gis.service.published_version` and `norms.service.effective_norm`.
# - `current_calculation(db, application_id) -> Calculation | None` — the
#   newest calculation for the application. 3.10 builds its invoice from
#   exactly this.
# - `set_status(db, application_id, *, to_status, actor=None, reason=None) ->
#   Application` — the ONE way a level-4 module moves an application: 3.10
#   writes INVOICED, PAID, EXPIRED_UNPAID; 3.11 writes PERMIT_ISSUED, CLOSED.
#   It validates the transition against tz/05, writes the history row and
#   the audit entry, and refuses an illegal jump.
# - The four event names in `applications.events`: APPLICATION_SUBMITTED,
#   APPLICATION_APPROVED, APPLICATION_REJECTED, APPLICATION_CANCELLED.
#
# The signature identities (ruling 25), because 3.11 collects signatures and
# must not guess: a SUBMISSION is signed as `("application_submission", <the
# SUBMITTED history row's id>, "application_submit")`; a DECISION is signed
# as `("application", <application id>, "application_decision")`. An
# application with three submission attempts has three separate signature
# objects and one decision object.
#
# A level-4 caller must NEVER import `applications.repo` or
# `applications.models`, and must never UPDATE `applications.status`
# directly — `set_status` exists so that every transition in the system has
# one implementation, one history row and one audit entry.
#
# No consumer reads a check as singular. The card returns `checks` as the
# full list and the reviewer's screen shows the history (ruling 12). Do NOT
# add a `latest_check` helper or a `DISTINCT ON (check_type)` read: 3.10
# reading "the newest calculation" is correct because a calculation is a
# price and only the last one is owed; a check is evidence, and evidence is
# a list.


async def get(db: AsyncSession, application_id: uuid.UUID) -> Application | None:
    """The application row, or `None`. No permission or zone rule: the
    caller is another SERVICE inside this process, mirroring
    `gis.service.published_version` and `norms.service.effective_norm`."""
    return await repo.get_application(db, application_id)


async def current_calculation(db: AsyncSession, application_id: uuid.UUID) -> Calculation | None:
    """The newest calculation for the application — what 3.10 builds its
    invoice from. Delegates to `norms.service.latest_calculation` (ruling
    C6): `applications` is level 3 and may call `norms` (level 2) only
    through its service, never by querying `calculations` itself or
    importing `norms.repo` (module boundary, CLAUDE.md)."""
    return await norms_service.latest_calculation(db, application_id)


async def set_status(
    db: AsyncSession,
    application_id: uuid.UUID,
    *,
    to_status: str,
    actor: User | None = None,
    reason: str | None = None,
) -> Application:
    """The ONE way a level-4 module moves an application (public surface
    above): validates the transition against tz/05, writes the history row
    and the audit entry, and refuses an illegal jump.

    Touches ONLY `applications.status` — `decided_at` and any other flow
    timestamp belong to the flow action that owns it (branch 2), never to
    this function.

    Frozen signature (controller ruling C5): do not add parameters, do not
    make `to_status` positional, do not add an `action`/`id` override.
    Branch 2's `submit` needs to supply the SUBMITTED history row's id
    explicitly (ruling 25, the signature-identity note above), which this
    signature cannot express — `submit` writes its own transition instead of
    routing through here, and stays audited under its own flow-verb constant
    (ruling 17: `APPLICATION_SUBMIT` and siblings), never
    `APPLICATION_STATUS_CHANGE`.
    """
    application = await get(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})

    _assert_transition(application, to_status)
    from_status = application.status
    application.status = to_status

    history = ApplicationStatusHistory(
        application_id=application.id,
        from_status=from_status,
        to_status=to_status,
        changed_by=actor.id if actor else None,
        reason_text=reason,
    )
    await repo.add_status_history(db, history)
    # Same `onupdate=func.now()` expiry a plain UPDATE leaves behind (lesson:
    # "the row in memory is not what Postgres stored") — refresh before a
    # caller reads `updated_at` off the returned row.
    await db.refresh(application)

    await audit.log(
        db,
        action=APPLICATION_STATUS_CHANGE,
        user_id=actor.id if actor else None,
        object_type="application",
        object_id=application.id,
        old_value={"status": from_status},
        new_value={"status": to_status},
    )
    return application
