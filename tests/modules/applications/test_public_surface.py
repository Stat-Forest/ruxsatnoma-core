"""Direct in-process tests for Task 8's public surface, the three functions
branch 1 ships: `service.get`, `service.current_calculation`,
`service.set_status`. Nothing in this branch calls them over HTTP — the
callers are 3.10a `payments` and 3.11 `permits`, running in parallel right
now — so an end-to-end/HTTP scenario would exercise nothing here at all
(lesson: "a 'public surface' task's own end-to-end test can ship the surface
untested"). Every function gets its own direct call below."""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.errors import DomainError
from app.db import make_session_factory
from app.modules.applications import service as applications_service
from app.modules.applications.models import (
    APPLICATION_STATUSES,
    Application,
    ApplicationStatusHistory,
)
from app.modules.applications.service import APPLICATION_STATUS_CHANGE, APPLICATION_TRANSITIONS
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Applicant, User
from app.modules.norms import service as norms_service
from app.modules.norms.models import Calculation


async def _draft(db: AsyncSession, applicant: Applicant) -> Application:
    """The minimal `applications` row (ruling 7: everything else is
    nullable) — mirrors `test_models.py`'s own `_app` helper, kept local to
    this file per this codebase's per-file-helper convention
    (`test_calculations_api.py` does the same rather than importing across
    test modules)."""
    row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
    )
    db.add(row)
    await db.flush()
    return row


# --- the transition table itself --------------------------------------------


def test_transition_table_has_all_fourteen_statuses_and_only_real_targets() -> None:
    """The one-source-of-truth discipline `models.py`'s own docstring
    describes for the CHECK constraints: a status added to
    `APPLICATION_STATUSES` and not here, or a typo'd target, must fail this
    before it ships."""
    assert set(APPLICATION_TRANSITIONS) == set(APPLICATION_STATUSES)
    for status, targets in APPLICATION_TRANSITIONS.items():
        assert targets <= set(APPLICATION_STATUSES), status


def test_archived_is_the_only_terminal_status() -> None:
    terminal = [status for status, targets in APPLICATION_TRANSITIONS.items() if not targets]
    assert terminal == ["ARCHIVED"]


def test_no_status_transitions_to_itself() -> None:
    """3.10's retry paths will attempt exactly this (task brief) — must be
    illegal for every status, not only the ones `set_status` is exercised
    against below."""
    for status, targets in APPLICATION_TRANSITIONS.items():
        assert status not in targets


# --- service.get -------------------------------------------------------------


async def test_get_returns_none_for_an_unknown_id(db: AsyncSession) -> None:
    assert await applications_service.get(db, uuid.uuid4()) is None


async def test_get_returns_the_row(db: AsyncSession, applicant: Applicant) -> None:
    row = await _draft(db, applicant)
    fetched = await applications_service.get(db, row.id)
    assert fetched is not None
    assert fetched.id == row.id
    assert fetched.status == "DRAFT"


# --- service.current_calculation / norms.service.latest_calculation --------


async def test_current_calculation_is_none_with_no_calculations(
    db: AsyncSession, applicant: Applicant
) -> None:
    application = await _draft(db, applicant)
    assert await applications_service.current_calculation(db, application.id) is None
    # Agrees with the function it delegates to (ruling C6).
    assert await norms_service.latest_calculation(db, application.id) is None


async def test_current_calculation_is_the_newest_row(
    db: AsyncSession, applicant: Applicant, grazing_activity_id: uuid.UUID
) -> None:
    """Both rows land in the same transaction, so Postgres's `now()` (the
    `created_at` server default) gives them the IDENTICAL timestamp — the
    exact tie `id` (uuid7, time-ordered) exists to break, and the only way to
    prove the tie-break actually works rather than merely being untested."""
    application = await _draft(db, applicant)
    first = Calculation(
        application_id=application.id,
        activity_type_id=grazing_activity_id,
        rule_code_version="norms-1.0.0",
        input_snapshot={},
        amount=Decimal("1000.00"),
        breakdown={},
    )
    db.add(first)
    await db.flush()
    second = Calculation(
        application_id=application.id,
        activity_type_id=grazing_activity_id,
        rule_code_version="norms-1.0.0",
        input_snapshot={},
        amount=Decimal("2000.00"),
        breakdown={},
    )
    db.add(second)
    await db.flush()
    assert first.created_at == second.created_at  # confirms the tie is real
    assert first.id < second.id  # uuid7: insertion order agrees with id order

    latest = await applications_service.current_calculation(db, application.id)
    assert latest is not None
    assert latest.id == second.id
    assert latest.amount == Decimal("2000.00")

    direct = await norms_service.latest_calculation(db, application.id)
    assert direct is not None
    assert direct.id == second.id


async def test_current_calculation_ignores_other_applications(
    db: AsyncSession, applicant: Applicant, grazing_activity_id: uuid.UUID
) -> None:
    application_a = await _draft(db, applicant)
    application_b = await _draft(db, applicant)
    row = Calculation(
        application_id=application_b.id,
        activity_type_id=grazing_activity_id,
        rule_code_version="norms-1.0.0",
        input_snapshot={},
        amount=Decimal("500.00"),
        breakdown={},
    )
    db.add(row)
    await db.flush()

    assert await applications_service.current_calculation(db, application_a.id) is None
    result = await applications_service.current_calculation(db, application_b.id)
    assert result is not None
    assert result.id == row.id


# --- service.set_status -------------------------------------------------------
#
# These drive whatever legal edge is cheapest to reach from `_draft()` — mostly
# DRAFT -> SUBMITTED — because what is under test is the MECHANISM: the history
# row, the audit entry, the refusals, the lock and the re-read. They are not a
# model of a caller: `service.py`'s public-surface block limits a level-4 module
# to INVOICED/PAID/EXPIRED_UNPAID/PERMIT_ISSUED/CLOSED (final review I1), and
# every applicant/staff edge used below belongs to branch 2's flow verbs.


async def test_set_status_legal_transition_writes_history_and_audit(
    db: AsyncSession, applicant: Applicant, staff_user: User
) -> None:
    application = await _draft(db, applicant)  # DRAFT
    updated = await applications_service.set_status(
        db, application.id, to_status="SUBMITTED", actor=staff_user, reason="filed by staff"
    )

    assert updated.id == application.id
    assert updated.status == "SUBMITTED"

    history = (
        (
            await db.execute(
                select(ApplicationStatusHistory).where(
                    ApplicationStatusHistory.application_id == application.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(history) == 1
    assert history[0].from_status == "DRAFT"
    assert history[0].to_status == "SUBMITTED"
    assert history[0].changed_by == staff_user.id
    assert history[0].reason_text == "filed by staff"

    entries = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.object_type == "application", AuditLog.object_id == application.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1
    assert entries[0].action == APPLICATION_STATUS_CHANGE
    assert entries[0].user_id == staff_user.id
    assert entries[0].old_value == {"status": "DRAFT"}
    assert entries[0].new_value == {"status": "SUBMITTED"}


async def test_set_status_with_no_actor_leaves_changed_by_and_user_id_null(
    db: AsyncSession, applicant: Applicant
) -> None:
    """`changed_by=None`/`user_id=None` means "the system" (design/02) — a
    worker-driven transition (3.10's EXPIRED_UNPAID job, say) has no actor at
    all."""
    application = await _draft(db, applicant)
    updated = await applications_service.set_status(db, application.id, to_status="SUBMITTED")
    assert updated.status == "SUBMITTED"

    history = (
        await db.execute(
            select(ApplicationStatusHistory).where(
                ApplicationStatusHistory.application_id == application.id
            )
        )
    ).scalar_one()
    assert history.changed_by is None

    entry = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.object_type == "application", AuditLog.object_id == application.id
            )
        )
    ).scalar_one()
    assert entry.user_id is None


async def test_set_status_chains_through_several_legal_transitions(
    db: AsyncSession, applicant: Applicant, staff_user: User
) -> None:
    """DRAFT -> SUBMITTED -> IN_REVIEW -> APPROVED, the exact chain 3.9b's
    own review flow will drive one edge at a time; proves consecutive calls
    each read the row's OWN current status back correctly."""
    application = await _draft(db, applicant)
    for target in ("SUBMITTED", "IN_REVIEW", "APPROVED"):
        updated = await applications_service.set_status(
            db, application.id, to_status=target, actor=staff_user
        )
        assert updated.status == target

    history = (
        (
            await db.execute(
                select(ApplicationStatusHistory)
                .where(ApplicationStatusHistory.application_id == application.id)
                .order_by(ApplicationStatusHistory.occurred_at, ApplicationStatusHistory.id)
            )
        )
        .scalars()
        .all()
    )
    assert [(h.from_status, h.to_status) for h in history] == [
        ("DRAFT", "SUBMITTED"),
        ("SUBMITTED", "IN_REVIEW"),
        ("IN_REVIEW", "APPROVED"),
    ]
    # The reason that ORDER BY carries `id` (final review M3): `occurred_at` is
    # `now()` = TRANSACTION start time, so all three rows share it exactly and
    # `occurred_at` alone would render them in arbitrary order. Not a corner
    # case — branch 2's `approve()` and 3.10a's in-transaction invoice handler
    # write two rows in one transaction by design (ruling 3а).
    assert len({h.occurred_at for h in history}) == 1


async def test_set_status_rejects_an_illegal_jump(db: AsyncSession, applicant: Applicant) -> None:
    application = await _draft(db, applicant)  # DRAFT
    with pytest.raises(DomainError) as exc_info:
        await applications_service.set_status(db, application.id, to_status="APPROVED")
    assert exc_info.value.code == "ERR-APP-004"
    assert exc_info.value.http_status == 409
    assert exc_info.value.details == {"reason": "bad_transition", "from": "DRAFT", "to": "APPROVED"}

    # No partial write: the application keeps its original status.
    reloaded = await applications_service.get(db, application.id)
    assert reloaded is not None
    assert reloaded.status == "DRAFT"


async def test_set_status_rejects_a_transition_to_the_current_status(
    db: AsyncSession, applicant: Applicant
) -> None:
    """3.10's retry paths will attempt exactly this (task brief) — DRAFT has
    no self-loop in tz/05's table, so it fails like any other illegal jump."""
    application = await _draft(db, applicant)
    with pytest.raises(DomainError) as exc_info:
        await applications_service.set_status(db, application.id, to_status="DRAFT")
    assert exc_info.value.code == "ERR-APP-004"


async def test_set_status_from_a_terminal_status_is_always_illegal(
    db: AsyncSession, applicant: Applicant
) -> None:
    application = await _draft(db, applicant)
    application.status = "ARCHIVED"  # direct ORM write, bypassing set_status on purpose
    await db.flush()

    with pytest.raises(DomainError) as exc_info:
        await applications_service.set_status(db, application.id, to_status="DRAFT")
    assert exc_info.value.code == "ERR-APP-004"
    assert exc_info.value.details == {"reason": "bad_transition", "from": "ARCHIVED", "to": "DRAFT"}


async def test_set_status_unknown_application_raises_not_found(db: AsyncSession) -> None:
    with pytest.raises(DomainError) as exc_info:
        await applications_service.set_status(db, uuid.uuid4(), to_status="SUBMITTED")
    assert exc_info.value.code == "ERR-SYS-003"


# --- service.set_status: the row lock (review C1) ---------------------------


async def test_set_status_reads_the_committed_status_not_this_sessions_cached_one(
    engine: AsyncEngine, db: AsyncSession, applicant: Applicant
) -> None:
    """Final review C2, the half the two-session lock test above cannot reach.

    That test opens two BRAND-NEW sessions, so neither holds the row in its
    identity map and both get a full population — the lock is proven, the
    re-read is not. The designed level-4 usage is the opposite shape: a caller
    runs `service.get(...)` to inspect the application and THEN calls
    `set_status(...)` on the same session. `with_for_update` alone would take
    the lock, load nothing (the loader refreshes only unloaded attributes for
    an instance already in the identity map) and validate the transition
    against the STALE cached status — writing a history row for a transition
    that never happened and overwriting a committed value.

    The concrete case: 3.10a's payment callback reads INVOICED, the expiry
    scheduler commits EXPIRED_UNPAID in between, and the callback then asks for
    PAID. INVOICED -> PAID is legal and EXPIRED_UNPAID -> PAID is not, so a
    stale read silently loses the scheduler's write; a correct read refuses.
    """
    application = await _draft(db, applicant)
    application.status = "INVOICED"
    await db.flush()
    await db.commit()  # a separate connection cannot see uncommitted work
    # Held as a plain value: `db.rollback()` below expires every instance, and
    # reading `application.id` off an expired one inside a synchronous SQLAlchemy
    # expression raises `MissingGreenlet` (lesson: "the row in memory is not what
    # Postgres stored").
    application_id = application.id

    # The read that plants the stale copy: `service.get` on THIS session, the
    # first thing every level-4 caller does.
    cached = await applications_service.get(db, application_id)
    assert cached is not None
    assert cached.status == "INVOICED"

    # Meanwhile, the expiry scheduler commits INVOICED -> EXPIRED_UNPAID on its
    # own session and finishes, so no lock is held by the time we ask.
    factory = make_session_factory(engine)
    async with factory() as scheduler:
        await applications_service.set_status(scheduler, application_id, to_status="EXPIRED_UNPAID")
        await scheduler.commit()

    # Still INVOICED in this session's identity map — `expire_on_commit=False`
    # (app/db.py) never cleared it. This is the trap, asserted so the test says
    # out loud what it is defending against.
    assert cached.status == "INVOICED"

    with pytest.raises(DomainError) as exc_info:
        await applications_service.set_status(db, application_id, to_status="PAID")
    assert exc_info.value.code == "ERR-APP-004"
    assert exc_info.value.details == {
        "reason": "bad_transition",
        "from": "EXPIRED_UNPAID",  # the committed status, never the cached one
        "to": "PAID",
    }

    # And nothing was written: no history row claiming a transition FROM
    # INVOICED that never happened, and the row is still EXPIRED_UNPAID.
    await db.rollback()
    rows = (
        await db.execute(
            select(ApplicationStatusHistory).where(
                ApplicationStatusHistory.application_id == application_id
            )
        )
    ).scalars()
    assert [row.to_status for row in rows] == ["EXPIRED_UNPAID"]
    final = await db.get(Application, application_id, populate_existing=True)
    assert final is not None
    assert final.status == "EXPIRED_UNPAID"


async def test_set_status_locks_the_row_so_a_concurrent_transition_loses(
    engine: AsyncEngine, db: AsyncSession, applicant: Applicant
) -> None:
    """The exact pair of writers C1 names: a payment callback moving
    INVOICED -> PAID and a scheduler job moving INVOICED -> EXPIRED_UNPAID,
    racing on one application. Two REAL, independent sessions (separate
    connections, mirroring `tests/workers/test_outbox_worker.py`'s own
    `make_session_factory(engine)` pattern) — a single session cannot
    demonstrate a row lock against itself.

    `application` must be COMMITTED, not merely flushed, before the two
    independent sessions are opened: a different connection cannot see
    another session's uncommitted work.
    """
    application = await _draft(db, applicant)
    application.status = "INVOICED"
    await db.flush()
    await db.commit()

    factory = make_session_factory(engine)
    session_a = factory()
    session_b = factory()
    try:
        # session_a takes the row lock (`get_application_for_update`'s
        # `SELECT ... FOR UPDATE`) and writes, but does NOT commit yet — the
        # lock is held for as long as its transaction stays open.
        updated_a = await applications_service.set_status(
            session_a, application.id, to_status="PAID"
        )
        assert updated_a.status == "PAID"

        # session_b starts concurrently, BEFORE session_a commits, so its
        # own `SELECT ... FOR UPDATE` must block on session_a's still-open
        # lock instead of reading the stale pre-write "INVOICED" and racing
        # it — the exact failure C1 describes.
        task = asyncio.create_task(
            applications_service.set_status(session_b, application.id, to_status="EXPIRED_UNPAID")
        )
        await asyncio.sleep(0.3)  # generous headroom for a localhost query
        assert not task.done(), "session_b should still be blocked on session_a's row lock"

        await session_a.commit()  # releases the lock

        with pytest.raises(DomainError) as exc_info:
            await asyncio.wait_for(task, timeout=5)
        # Unblocked, session_b re-reads the now-committed "PAID" — not the
        # stale "INVOICED" it originally saw — so PAID -> EXPIRED_UNPAID is
        # correctly refused as illegal, rather than silently overwriting
        # session_a's write.
        assert exc_info.value.code == "ERR-APP-004"
        assert exc_info.value.details == {
            "reason": "bad_transition",
            "from": "PAID",
            "to": "EXPIRED_UNPAID",
        }

        # The row genuinely ended up PAID, never EXPIRED_UNPAID — the exact
        # "the applicant paid but the row reads expired" outcome C1 warns of,
        # which this lock prevents rather than merely detects after the fact.
        final = await applications_service.get(db, application.id)
        assert final is not None
        await db.refresh(final)
        assert final.status == "PAID"
    finally:
        await session_a.rollback()
        await session_b.rollback()
        await session_a.close()
        await session_b.close()
