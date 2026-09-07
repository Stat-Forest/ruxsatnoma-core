"""Audit service: the only write path to audit_log for all modules.

Invariant (design/01 rule 6): the trail is written in the SAME transaction
as the action it records — this service never commits; the caller's
transaction (commit-on-success in get_db, decision #37) makes the action
and its trail durable atomically, or rolls back both.
"""

import uuid
from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import CORRELATION_ID_KEY
from app.modules.audit import repo
from app.modules.audit.models import AuditLog

# The one deliberate exception to the paragraph above ("audit... knows no
# domain vocabularies"), and it needs its own explanation. `applications`
# (level 3) writes this action when `decision._forward` escalates an
# over-limit application; `norms` (level 2) needs to test for the SAME fact,
# for ruling #107 (`decisions.md`), which gives a forwarding head read access
# to the application they escalated — `norms.service._may_read_calculation`
# must grant the identical exception over the CALCULATION bound to it, or the
# head can read the case but not the price (F7, `docs/plans/07.4-findings.md`).
# `norms` may not import ANYTHING from `applications` — module levels run the
# other way (`docs/design/01-struktura-monolita.md`) — so the token cannot
# stay defined only in `applications.decision` the way `.APPROVE`/`.REJECT`
# do. `audit` is level 0, already an ordinary dependency of BOTH modules, and
# is the lowest point either one reaches: defining it once here, rather than
# copying the literal into `norms` as a second definition of "was this actor
# the one who forwarded it", is exactly the fix F7 asked for and rejected
# doing the cheap way. `applications.decision` imports this name back
# (`decision.APPLICATION_FORWARD` keeps resolving to the same value; see its
# own import) instead of defining it — one constant, two readers.
APPLICATION_FORWARD = "application.forward"


async def log(
    db: AsyncSession,
    *,
    action: str,
    user_id: uuid.UUID | None = None,
    object_type: str | None = None,
    object_id: uuid.UUID | None = None,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    basis: str | None = None,
    result: Literal["success", "denied", "error"] = "success",
    ip: str | None = None,
    user_agent: str | None = None,
    correlation_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one audit entry within the caller's transaction.

    `action` follows "<object>.<verb>" in English ("application.submit",
    "user.block", "export.run"); constants belong to the acting module —
    audit is level 0 and knows no domain vocabularies. `user_id` is None
    for system/worker actions. When `correlation_id` is not given, the
    request id bound by the correlation middleware is picked up from
    structlog contextvars (workers pass their own explicitly).

    A raised exception rolls back the caller's transaction INCLUDING this
    trail — so `result="denied"`/`"error"` entries survive only on
    non-raising paths or via a separate transaction; denied/error entries use
    the early-commit pattern (decision #40 ruling 2): write the trail, commit
    explicitly, then raise.
    """
    if correlation_id is None:
        correlation_id = structlog.contextvars.get_contextvars().get(CORRELATION_ID_KEY)
    entry = AuditLog(
        action=action,
        user_id=user_id,
        object_type=object_type,
        object_id=object_id,
        old_value=old_value,
        new_value=new_value,
        basis=basis,
        result=result,
        ip=ip,
        user_agent=user_agent,
        correlation_id=correlation_id,
        extra=extra,
    )
    await repo.add(db, entry)
    return entry


async def already_logged(
    db: AsyncSession, *, action: str, object_type: str, object_id: uuid.UUID
) -> bool:
    """Whether `object_id` already has an `audit_log` row for `action` — the
    once-only check a periodic job needs before writing a risk-indicator row
    a second time (mirrors `notifications.service.already_notified`'s own
    shape and its own reasoning: a caller outside `audit` may not query
    `AuditLog` directly, and needs no new column to ask "was this already
    flagged"). First caller: `payments.jobs.refund_sla_sweep` (3.10b task
    10), whose RI-07 is a fact about the refund, not a notification, so
    `already_notified` does not apply to it.

    `object_type` is REQUIRED: `repo.exists` is served by `ix_audit_log_object`
    (`object_type`, `object_id`, `occurred_at`) only when `object_type` is
    supplied as that index's leading column — omit it and PostgreSQL falls
    back to a full `Seq Scan on audit_log`, confirmed by `EXPLAIN` (fix round
    1). `audit_log` is append-only and never shrinks, so this is not a
    theoretical cost."""
    return await repo.exists(db, action=action, object_type=object_type, object_id=object_id)


async def logged_by(
    db: AsyncSession, *, action: str, object_type: str, object_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Whether `user_id` is the actor behind an EXISTING `audit_log` row for
    this `(action, object_type, object_id)` triple — `already_logged`'s own
    triple, narrowed to one actor.

    First caller: `norms.service._forwarded_here_by`, mirroring
    `applications.service._forwarded_here_by` across the module boundary that
    stops it calling that function directly (see `APPLICATION_FORWARD`
    above). `applications.decision._forward` writes the matching
    `audit_log` row (`action=APPLICATION_FORWARD, user_id=actor.id,
    object_type="application", object_id=application.id`) in the SAME
    transaction as the `application_status_history` row
    `applications.service._forwarded_here_by` reads. Two rows, not one, and
    saying so matters: what F7 refused was two independent DEFINITIONS of "was
    forwarded by", and what remains here is one definition — this action token —
    answered from two rows a single function writes together. The residual risk
    is `_forward` one day writing one row and not the other, and each row is
    pinned by a test that fails the moment it stops being written."""
    return await repo.exists(
        db, action=action, object_type=object_type, object_id=object_id, user_id=user_id
    )
