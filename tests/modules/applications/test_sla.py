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

from app.modules.admin.models import Organization
from app.modules.applications import sla
from app.modules.applications.models import Application
from tests.modules.auth.test_sessions import make_user


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


@pytest.fixture
async def org_head_id(db: AsyncSession, leshoz: Organization) -> uuid.UUID:
    """A SECOND staff member holding `executor_head` in the SAME `leshoz` as
    `application_in_review`'s own `hodim_user` — fix round 1's "head" half of
    the reminder's two recipients (`tz/04` scenario line 45). `leshoz` is
    function-scoped and pytest caches it per test, so requesting it here and
    through `application_in_review`'s own chain (`hodim_user` depends on it
    too) resolves to the SAME organization row."""
    head = await make_user(db, role_code="executor_head", organization_id=leshoz.id)
    await db.flush()
    return head.id


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


async def test_the_sweep_reminds_the_executor_and_the_head_not_the_applicant(
    db, application_due_in_two_days, org_head_id
) -> None:
    """Fix round 1 (Important finding): `tz/04` scenario line 45 —
    «приближение SLA -> напоминание исполнителю и руководителю». The
    applicant cannot make the office decide any faster, so a reminder to
    them changes nothing while the deadline it warns about passes — the
    ASSIGNED EXECUTOR and the head of the application's organization are the
    only two recipients who can act on it.

    Runs the sweep TWICE, same as the RI-07 test above: `already_notified`'s
    `recipient_user_id` filter (fix round 1) must guard EACH recipient on
    its own, or the executor's own row would make the head look
    already-reminded on the very first pass, not just the second."""
    from sqlalchemy import func, select

    from app.modules.applications import jobs
    from app.modules.notifications.models import Notification

    application = await db.get(Application, application_due_in_two_days)
    assert application is not None
    executor_id = application.assigned_user_id
    applicant_id = application.submitted_by_user_id
    assert executor_id is not None

    await jobs.sla_sweep(db)
    await jobs.sla_sweep(db)

    rows = (
        await db.execute(
            select(Notification.recipient_user_id, func.count())
            .where(
                Notification.object_id == application_due_in_two_days,
                Notification.event_code == "application.sla_approaching",
                Notification.channel == "inapp",
            )
            .group_by(Notification.recipient_user_id)
        )
    ).all()
    counts = dict(rows)

    assert counts == {executor_id: 1, org_head_id: 1}
    assert applicant_id not in counts


async def test_a_reminder_re_fires_when_a_pause_moves_the_deadline(
    db, application_due_in_two_days, hodim_client, applicant_client, frozen_clock
) -> None:
    """Final whole-branch review, IMPORTANT. `already_notified`'s once-only
    key carried no time component at all, so the office was reminded AT MOST
    ONCE per application, EVER — and the one reminder it got named a deadline
    a later pause could already have moved past. `request_info`/
    `respond_info` read the pause's two endpoints through the module's own
    `_now()` (`frozen_clock`'s target, never `applications.jobs`'s own wall
    clock, which the sweep still reads for real): advancing it by EXACTLY 24
    hours between the two calls moves the shifted deadline's calendar DATE by
    exactly one day, deterministically, whatever time of day the test happens
    to run at — `sla.shift_deadline` adds the pause's length outright, and a
    full day added to any UTC instant always lands on the next date."""
    from sqlalchemy import func, select

    from app.modules.applications import jobs
    from app.modules.applications.models import Application
    from app.modules.notifications.models import Notification

    application = await db.get(Application, application_due_in_two_days)
    assert application is not None
    executor_id = application.assigned_user_id
    assert executor_id is not None

    async def _reminder_count() -> int:
        return (
            await db.scalar(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.object_id == application_due_in_two_days,
                    Notification.event_code == "application.sla_approaching",
                    Notification.channel == "inapp",
                    Notification.recipient_user_id == executor_id,
                )
            )
        ) or 0

    await jobs.sla_sweep(db)
    await jobs.sla_sweep(db)
    assert await _reminder_count() == 1, (
        "a second sweep against the SAME deadline must not remind a second time"
    )

    asked = await hodim_client.post(
        f"/api/v1/applications/{application_due_in_two_days}/request-info",
        json={"message": "Уточните состав стада"},
    )
    assert asked.status_code == 200, asked.text

    frozen_clock.advance(timedelta(hours=24))

    answered = await applicant_client.post(
        f"/api/v1/applications/{application_due_in_two_days}/respond-info",
        json={"text": "Готово", "file_ids": []},
    )
    assert answered.status_code == 200, answered.text

    # `respond_info` ran on `applicant_client`'s OWN session, over the same
    # row this test's `db` session already holds in its identity map (the
    # `db.get` above) — a bare re-query would hand `sla_sweep` back that
    # SAME cached, now-stale object rather than the shifted deadline Postgres
    # actually stored (lesson: "the row in memory is not what Postgres
    # stored"). An explicit refresh is what makes the sweep see the moved
    # deadline at all, not merely what this assertion is about.
    await db.refresh(application)

    await jobs.sla_sweep(db)
    assert await _reminder_count() == 2, (
        "a pause that moved the deadline forward must earn a fresh reminder"
    )
