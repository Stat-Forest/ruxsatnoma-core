"""Pure SLA arithmetic (plan `03.9b-applications-review` task 2, ruling 8).

Neither function below touches the database or the wall clock — the caller
(`applications.jobs.sla_sweep`) supplies `now`, exactly like `payments.jobs`'s
own sweeps do for their own deadlines.
"""

from datetime import datetime, timedelta

# The two statuses where the SLA clock is actually running: `SUBMITTED` (the
# office is holding the file, whether or not a reviewer has picked it up
# yet with `start_review`) and `IN_REVIEW` (a reviewer is actively working
# it). Every other status means the clock is either PAUSED (`PENDING_INFO` —
# the applicant, not the office, is who is being waited on; `RETURNED` is
# the same shape, and `submit()` deliberately KEEPS the existing
# `submitted_at`/`sla_deadline_at` on a resubmission rather than giving it a
# fresh one — ruling 16.1, `service.submit` itself) or DECIDED/terminal
# (`APPROVED`, `REJECTED`, `CANCELLED` and everything downstream of them) —
# none of those can be "overdue" in the sense RI-07 means it.
SLA_ACTIVE_STATUSES = ("SUBMITTED", "IN_REVIEW")


def shift_deadline(deadline: datetime, *, paused_for: timedelta) -> datetime:
    """Ruling 8: a CLOSED pause moves the stored deadline forward by its own
    length, so every reader sees one column instead of joining
    `info_requests` and summing every past pause. Whole seconds, never
    rounded to a day — a five-minute question asked at 23:55 must not buy a
    free day."""
    return deadline + paused_for


def is_overdue(status: str, deadline: datetime, now: datetime) -> bool:
    """True only while the clock is actually running (`SLA_ACTIVE_STATUSES`)
    AND the deadline has already passed. An OPEN pause (`PENDING_INFO`)
    suspends the clock whatever the stored deadline says, and a
    decided/terminal status is never overdue however stale its own deadline
    is left."""
    return status in SLA_ACTIVE_STATUSES and now > deadline
