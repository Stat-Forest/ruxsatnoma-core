"""RI-07 — the application SLA sweep (plan `03.9b-applications-review` task
2, `tz/10`, `applications.jobs.sla_sweep`). Mirrors `payments/
test_refund_sweep.py`'s own shape: every idempotency assertion below is
scoped to the ONE application its own fixture created — the shared,
persistent test database may carry other applications, some possibly overdue
too, and RI-07 has a SECOND producer (`payments.jobs.
RISK_INDICATOR_REFUND_OVERDUE`) that could otherwise pollute a bare count."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications import sla
from app.modules.applications.models import Application


def test_a_pause_moves_the_deadline_forward_by_its_own_length() -> None:
    """Ruling 8: the stored deadline is shifted when the pause CLOSES, so every
    reader sees one column instead of joining info_requests and summing."""
    deadline = datetime(2027, 5, 16, 12, 0, tzinfo=UTC)
    assert sla.shift_deadline(deadline, paused_for=timedelta(days=2, hours=3)) == datetime(
        2027, 5, 18, 15, 0, tzinfo=UTC
    )


def test_pauses_are_whole_seconds_not_rounded_to_days() -> None:
    """A five-minute question asked at 23:55 must not buy a free day."""
    deadline = datetime(2027, 5, 16, 12, 0, tzinfo=UTC)
    assert sla.shift_deadline(deadline, paused_for=timedelta(minutes=5)) == datetime(
        2027, 5, 16, 12, 5, tzinfo=UTC
    )


def test_an_application_awaiting_information_is_never_overdue() -> None:
    """Ruling 8: an OPEN pause suspends the clock whatever the stored deadline
    says — the applicant, not the reviewer, is the one being waited on."""
    past = datetime(2027, 5, 1, tzinfo=UTC)
    now = datetime(2027, 6, 1, tzinfo=UTC)
    assert sla.is_overdue("IN_REVIEW", past, now) is True
    assert sla.is_overdue("PENDING_INFO", past, now) is False


def test_a_decided_application_is_never_overdue() -> None:
    past = datetime(2027, 5, 1, tzinfo=UTC)
    now = datetime(2027, 6, 1, tzinfo=UTC)
    for status in ("APPROVED", "REJECTED", "CANCELLED"):
        assert sla.is_overdue(status, past, now) is False


@pytest.fixture
async def overdue_application(db: AsyncSession, application_in_review: str) -> uuid.UUID:
    """`application_in_review`, its deadline backdated directly. SLA
    arithmetic is a DATA field, not a status — setting it directly is the
    established shape for this class of fixture
    (`payments/test_refund_sweep.py::overdue_refund`), unlike `status`, which
    must always move through the real transition (lesson)."""
    application_id = uuid.UUID(application_in_review)
    application = await db.get(Application, application_id)
    assert application is not None
    application.sla_deadline_at = datetime.now(UTC) - timedelta(days=1)
    await db.flush()
    return application_id


@pytest.fixture
async def application_due_in_two_days(db: AsyncSession, application_in_review: str) -> uuid.UUID:
    """`application_in_review`, its deadline moved to two days out — inside
    `applications.jobs.SLA_REMINDER_DAYS_BEFORE`'s window."""
    application_id = uuid.UUID(application_in_review)
    application = await db.get(Application, application_id)
    assert application is not None
    application.sla_deadline_at = datetime.now(UTC) + timedelta(days=2)
    await db.flush()
    return application_id


async def test_the_sweep_logs_ri_07_once_and_only_once(db, overdue_application) -> None:
    """Ruling 2: RI-07 goes into the audit journal for oversight (4.2) to
    harvest. The job runs daily — a second pass must not log it again."""
    from sqlalchemy import select

    from app.modules.applications import jobs
    from app.modules.audit.models import AuditLog

    await jobs.sla_sweep(db)
    await jobs.sla_sweep(db)

    entries = (
        await db.scalars(select(AuditLog).where(AuditLog.object_id == overdue_application))
    ).all()
    breaches = [e for e in entries if (e.extra or {}).get("risk_indicator") == "RI-07"]
    assert len(breaches) == 1


async def test_the_sweep_reminds_before_the_deadline_once(db, application_due_in_two_days) -> None:
    from sqlalchemy import func, select

    from app.modules.applications import jobs
    from app.modules.notifications.models import Notification

    await jobs.sla_sweep(db)
    await jobs.sla_sweep(db)

    sent = await db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.object_id == application_due_in_two_days,
            Notification.event_code == "application.sla_approaching",
        )
    )
    assert sent == 1
