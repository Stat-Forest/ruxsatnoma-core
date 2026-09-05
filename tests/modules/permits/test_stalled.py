"""Task 7, ruling 16: the sweep that reports a permit nobody signed, and moves
nothing.

`tz/12` #16 is open with the Agency: a paid permit whose recipient never signs
cannot be revoked or expired today, and this stage does not close that gap —
`jobs.watch_stalled_permits` only makes the state visible. The one guard test
below (`test_pending_signatures_still_has_exactly_one_exit`) pins the reason
this file exists at all: if that assertion is ever loosened, `tz/12` #16 has
been answered and `tz/05` must be updated in the very same commit.
"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import User
from app.modules.permits import events, jobs, service
from app.modules.permits.models import Permit
from tests.modules.permits.conftest import notification_rows


async def test_a_permit_nobody_signed_is_reported_once(
    db: AsyncSession,
    issued_permit: Permit,
    assigned_executor: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling 16: visibility, not a transition. The permit does not move."""
    monkeypatch.setattr(jobs, "business_today", lambda: issued_permit.period_to + timedelta(days=1))
    await jobs.watch_stalled_permits(db)
    await db.refresh(issued_permit)
    assert issued_permit.status == "pending_signatures"

    rows = await notification_rows(db, object_id=issued_permit.id)
    stalled = [r for r in rows if r.event_code == events.PERMIT_UNSIGNED_STALLED]
    assert len(stalled) == 1
    assert stalled[0].recipient_user_id == assigned_executor.id

    await jobs.watch_stalled_permits(db)  # a second night
    rows = await notification_rows(db, object_id=issued_permit.id)
    assert len([r for r in rows if r.event_code == events.PERMIT_UNSIGNED_STALLED]) == 1


async def test_a_permit_not_yet_due_is_not_reported(
    db: AsyncSession,
    issued_permit: Permit,
    assigned_executor: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`period_to` is INCLUSIVE — the same reading `permits_ending_before`
    documents. The permit's own last day is not yet stale."""
    monkeypatch.setattr(jobs, "business_today", lambda: issued_permit.period_to)
    await jobs.watch_stalled_permits(db)

    rows = await notification_rows(db, object_id=issued_permit.id)
    assert not [r for r in rows if r.event_code == events.PERMIT_UNSIGNED_STALLED]


async def test_a_stalled_permit_with_no_assigned_executor_is_skipped(
    db: AsyncSession, issued_permit: Permit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`make_paid_application` leaves `assigned_user_id` null on purpose
    (conftest). Nobody to tell: log and skip, the shape
    `subscribers.on_payment_confirmed` already uses — not a raised error that
    would cost the batch its neighbours.

    Scoped to THIS permit's own notification row, never to the batch's
    `scanned`/`processed` counts: this test DB is shared and persistent
    (conftest's own lesson), so another test's stray `pending_signatures`
    permit can easily fall due under this test's own advanced clock too.
    """
    monkeypatch.setattr(jobs, "business_today", lambda: issued_permit.period_to + timedelta(days=1))
    await jobs.watch_stalled_permits(db)  # must not raise despite no recipient

    rows = await notification_rows(db, object_id=issued_permit.id)
    assert not [r for r in rows if r.event_code == events.PERMIT_UNSIGNED_STALLED]


async def test_watching_writes_no_status_history_and_no_audit_row(
    db: AsyncSession,
    issued_permit: Permit,
    assigned_executor: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole deliverable of ruling 16: notify, and write NOTHING else — no
    NEW `permit_status_history` row, no NEW `audit_log` entry, no status
    change. Compared before/after rather than against zero: issuance itself
    already wrote one history row (`-> pending_signatures`) and one audit row
    (`permit.issue`) for this very permit before the sweep ever runs."""
    from app.modules.audit.models import AuditLog
    from app.modules.permits.models import PermitStatusHistory

    async def _counts() -> tuple[int, int]:
        history = await db.scalar(
            select(func.count())
            .select_from(PermitStatusHistory)
            .where(PermitStatusHistory.permit_id == issued_permit.id)
        )
        audits = await db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.object_id == issued_permit.id)
        )
        return history or 0, audits or 0

    before = await _counts()

    monkeypatch.setattr(jobs, "business_today", lambda: issued_permit.period_to + timedelta(days=1))
    await jobs.watch_stalled_permits(db)

    assert await _counts() == before

    await db.refresh(issued_permit)
    assert issued_permit.status == "pending_signatures"


async def test_the_scheduler_runs_the_stalled_watch() -> None:
    """A job nothing schedules is a function, not a sweep — the same guard
    `test_the_scheduler_runs_both_sweeps` holds for the permit pair."""
    from app.workers.scheduler import build_scheduler

    ids = {job.id for job in build_scheduler(None).get_jobs()}  # type: ignore[arg-type]
    assert "watch_stalled_permits" in ids


async def test_pending_signatures_still_has_exactly_one_exit(db: AsyncSession) -> None:
    """The Agency has not answered `tz/12` #16 and this stage did not answer it
    for them. If this assertion is ever changed, `tz/12` #16 must be closed in
    the same commit and `tz/05` updated."""
    assert service.PERMIT_TRANSITIONS["pending_signatures"] == frozenset({"active"})
