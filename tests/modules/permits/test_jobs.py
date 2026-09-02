"""Task 7: the two daily sweeps — a permit that runs out, and the application it
then closes.

Both run behind the advisory lock stage 3.4's scheduler already holds, so nothing
here starts a scheduler; the jobs take a session and are driven directly, the way
`tests/workers/test_jobs.py` drives the four that came before them.

Every fixture below builds its permit through the ORM. Nothing in 3.11a WRITES
`expired` except the job under test, so a fixture that went through the service
would be the code it is supposed to check; and the contour needs no published
version at all here, unlike `test_providers.py`'s — these queries read `permits`
and never touch geometry.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import MediaFile
from app.core.time import business_today
from app.modules.admin.models import Organization
from app.modules.applications.models import Application, ApplicationStatusHistory
from app.modules.gis.models import Contour, GisLayer
from app.modules.notifications.models import Notification
from app.modules.permits import events
from app.modules.permits.models import Permit, PermitStatusHistory
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt
from tests.modules.permits.conftest import make_permit_on_contour


@pytest.fixture
async def contour(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, approval_doc: MediaFile
) -> Contour:
    """A contour with one version, for `permits.contour_version_id` to point at.
    Deliberately not published: nothing in these two sweeps reads geometry, and a
    fixture that published would be asserting something it does not test."""
    row = await make_contour(db, contours_layer, leshoz)
    await make_version(db, row.id, random_box_wkt(), approval_doc_id=approval_doc.id)
    return row


@pytest.fixture
async def version_id(db: AsyncSession, contour: Contour) -> uuid.UUID:
    from app.modules.gis.models import ContourVersion

    return (
        await db.execute(select(ContourVersion.id).where(ContourVersion.contour_id == contour.id))
    ).scalar_one()


async def _permit(
    db: AsyncSession,
    *,
    contour: Contour,
    version_id: uuid.UUID,
    org: Organization,
    activity_type_id: uuid.UUID,
    status: str,
    period_to: date,
) -> Permit:
    return await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=org,
        activity_type_id=activity_type_id,
        status=status,
        period_from=period_to - timedelta(days=120),
        period_to=period_to,
    )


@pytest.fixture
async def active_permit_ending_yesterday(
    db: AsyncSession,
    contour: Contour,
    version_id: uuid.UUID,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> Permit:
    return await _permit(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="active",
        period_to=business_today() - timedelta(days=1),
    )


@pytest.fixture
async def active_permit_ending_today(
    db: AsyncSession,
    contour: Contour,
    version_id: uuid.UUID,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> Permit:
    return await _permit(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="active",
        period_to=business_today(),
    )


@pytest.fixture
async def expired_permit(
    db: AsyncSession,
    contour: Contour,
    version_id: uuid.UUID,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> Permit:
    """A permit already finished, with its application still in `PERMIT_ISSUED`
    — the state `close_finished` exists to resolve."""
    return await _permit(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="expired",
        period_to=business_today() - timedelta(days=10),
    )


async def _reread_application(db: AsyncSession, permit: Permit) -> Application:
    """One helper for every assertion about the application, because
    `applications.service.get` is `db.get` and issues no SELECT for a row the
    session already holds (lesson) — a remembered `db.refresh` is what failed
    twice in this package."""
    application = await db.get(Application, permit.application_id)
    assert application is not None
    await db.refresh(application)
    return application


async def _expired_history_count(db: AsyncSession, permit: Permit) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(PermitStatusHistory)
            .where(
                PermitStatusHistory.permit_id == permit.id,
                PermitStatusHistory.to_status == "expired",
            )
        )
    ) or 0


async def test_a_permit_past_its_period_expires(
    db: AsyncSession, active_permit_ending_yesterday: Permit
) -> None:
    from app.modules.permits import jobs

    await jobs.expire_permits(db)
    await db.refresh(active_permit_ending_yesterday)
    assert active_permit_ending_yesterday.status == "expired"


async def test_a_permit_ending_today_is_still_active(
    db: AsyncSession, active_permit_ending_today: Permit
) -> None:
    """`period_to` is inclusive — the last day is a day of use, not a day past."""
    from app.modules.permits import jobs

    await jobs.expire_permits(db)
    await db.refresh(active_permit_ending_today)
    assert active_permit_ending_today.status == "active"


async def test_the_sweep_reads_the_tashkent_date_not_the_servers(
    db: AsyncSession,
    contour: Contour,
    version_id: uuid.UUID,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`date.today()` follows the SERVER's zone and reports YESTERDAY for five
    hours a day on a UTC container (lesson) — which on this sweep means expiring
    a permit on its own last day, a day early, every night between 19:00 and
    midnight Tashkent. Patched in the CALLING module's namespace, never in
    `app.core.time` (the seeded-dates lesson's own instruction)."""
    from app.modules.permits import jobs

    permit = await _permit(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="active",
        period_to=date(2027, 6, 15),
    )
    monkeypatch.setattr(jobs, "business_today", lambda: date(2027, 6, 15))
    await jobs.expire_permits(db)
    await db.refresh(permit)
    assert permit.status == "active", "the permit's own last day is still a day of use"

    monkeypatch.setattr(jobs, "business_today", lambda: date(2027, 6, 16))
    await jobs.expire_permits(db)
    await db.refresh(permit)
    assert permit.status == "expired"


async def test_the_expiry_sweep_is_idempotent(
    db: AsyncSession, active_permit_ending_yesterday: Permit
) -> None:
    from app.modules.permits import jobs

    await jobs.expire_permits(db)
    await jobs.expire_permits(db)

    assert await _expired_history_count(db, active_permit_ending_yesterday) == 1


async def test_the_expiry_sweep_leaves_a_permit_awaiting_signatures_alone(
    db: AsyncSession,
    contour: Contour,
    version_id: uuid.UUID,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> None:
    """Only `active` expires. A permit nobody finished signing never came into
    force, so «муддати тугаган» would be a false statement about it on the public
    check page — and `suspended`/`revoked` belong to 3.11b, which owns what
    happens to a suspended permit whose period runs out."""
    from app.modules.permits import jobs

    for status in ("pending_signatures", "suspended", "revoked"):
        permit = await _permit(
            db,
            contour=contour,
            version_id=version_id,
            org=leshoz,
            activity_type_id=grazing_activity_id,
            status=status,
            period_to=business_today() - timedelta(days=1),
        )
        await jobs.expire_permits(db)
        await db.refresh(permit)
        assert permit.status == status


async def test_a_finished_permit_closes_its_application(
    db: AsyncSession, expired_permit: Permit
) -> None:
    from app.modules.permits import jobs

    await jobs.close_finished(db)
    assert (await _reread_application(db, expired_permit)).status == "CLOSED"


async def test_the_closure_sweep_is_idempotent(db: AsyncSession, expired_permit: Permit) -> None:
    from app.modules.permits import jobs

    await jobs.close_finished(db)
    await jobs.close_finished(db)
    assert (await _reread_application(db, expired_permit)).status == "CLOSED"


async def test_a_revoked_permit_closes_its_application_too(
    db: AsyncSession,
    contour: Contour,
    version_id: uuid.UUID,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> None:
    """`revoked` is 3.11b's status, and this sweep is written to accept it now:
    tz/05 reaches CLOSED from PERMIT_ISSUED whichever way the permit ended."""
    from app.modules.permits import jobs

    permit = await _permit(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="revoked",
        period_to=business_today() + timedelta(days=30),
    )
    await jobs.close_finished(db)
    assert (await _reread_application(db, permit)).status == "CLOSED"


async def test_the_closure_sweep_never_touches_a_live_permits_application(
    db: AsyncSession, active_permit_ending_today: Permit
) -> None:
    """The candidate set is the FINISHED permits, and nothing else. An active
    permit whose application is still `PERMIT_ISSUED` is the ordinary state of
    every permit in force."""
    from app.modules.permits import jobs

    await jobs.close_finished(db)
    assert (await _reread_application(db, active_permit_ending_today)).status == "PERMIT_ISSUED"


async def test_the_two_sweeps_run_back_to_back(
    db: AsyncSession, active_permit_ending_yesterday: Permit
) -> None:
    """The nightly pair, in the order the scheduler runs them: a permit that ran
    out last night is expired and its application closed by morning."""
    from app.modules.permits import jobs

    await jobs.expire_permits(db)
    await jobs.close_finished(db)

    await db.refresh(active_permit_ending_yesterday)
    assert active_permit_ending_yesterday.status == "expired"
    assert (await _reread_application(db, active_permit_ending_yesterday)).status == "CLOSED"


async def test_expiry_notifies_the_holder_once(
    db: AsyncSession, active_permit_ending_yesterday: Permit
) -> None:
    """`permit.expired` — DOTTED, a `notification_templates.event_code` and not a
    bus name (`permits/events.py`'s three-vocabularies table). Seeded by
    migration 0019 for both channels; without a template `notify` writes a raw
    fallback in-app and sends NOTHING by SMS, silently."""
    from app.modules.permits import jobs

    await jobs.expire_permits(db)
    await jobs.expire_permits(db)

    sent = await db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.object_id == active_permit_ending_yesterday.id,
            Notification.event_code == events.PERMIT_EXPIRED,
        )
    )
    assert sent == 1


async def test_the_expiry_is_audited_as_a_job(
    db: AsyncSession, active_permit_ending_yesterday: Permit
) -> None:
    """`user_id=None`, `correlation_id="job:<uuid>"` — the shape every periodic
    job in this codebase audits its data changes with (CLAUDE.md)."""
    from app.modules.audit.models import AuditLog
    from app.modules.permits import jobs, service

    await jobs.expire_permits(db)
    row = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.object_id == active_permit_ending_yesterday.id,
                AuditLog.action == service.PERMIT_EXPIRE,
            )
        )
    ).scalar_one()
    assert row.user_id is None
    assert row.correlation_id is not None and row.correlation_id.startswith("job:")


async def test_the_scheduler_runs_both_sweeps() -> None:
    """A job nothing schedules is a function, not a sweep. Registered on the same
    APScheduler instance stage 3.4 already runs behind a PG advisory lock (ruling
    16) — never a second mechanism."""
    from app.workers.scheduler import build_scheduler

    ids = {job.id for job in build_scheduler(None).get_jobs()}  # pyright: ignore[reportArgumentType]
    assert {"expire_permits", "close_finished_permits"} <= ids


async def test_an_expired_permit_frees_the_area_it_held(
    db: AsyncSession, active_permit_ending_yesterday: Permit, contour: Contour
) -> None:
    """Task 6 and Task 7 meet here: the sweep is what makes ruling 11's "an
    expired permit frees the area it held" happen on its own, rather than only
    when somebody remembers to move the status by hand."""
    from app.modules.permits import jobs, service

    before = await service.occupancy_provider(db, [contour.id])
    assert before[contour.id] == Decimal("12.5000")

    await jobs.expire_permits(db)

    after = await service.occupancy_provider(db, [contour.id])
    assert after.get(contour.id, Decimal("0")) == Decimal("0")


# --- review, Important 3: batches, cursors and one bad row --------------------


async def _expiring(
    db: AsyncSession,
    contour: Contour,
    version_id: uuid.UUID,
    leshoz: Organization,
    activity_type_id: uuid.UUID,
    count: int,
) -> list[Permit]:
    return [
        await _permit(
            db,
            contour=contour,
            version_id=version_id,
            org=leshoz,
            activity_type_id=activity_type_id,
            status="active",
            period_to=business_today() - timedelta(days=1),
        )
        for _ in range(count)
    ]


@pytest.fixture
async def sentinel_permit(
    db: AsyncSession,
    contour: Contour,
    version_id: uuid.UUID,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> Permit:
    """A permit created BEFORE the ones a batching test cares about, used only as
    the `after_id` those tests start from.

    `permits.id` is `uuid7` and therefore time-ordered, so a cursor set here
    excludes every row that already existed — this database is shared and
    persistent and a stranger's stale `active` permit would otherwise be the one
    a `limit=2` batch picked up (lesson). Its own status keeps it out of the
    candidate set regardless."""
    return await _permit(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="pending_signatures",
        period_to=business_today() - timedelta(days=1),
    )


async def test_a_batch_stops_at_its_limit_and_says_where_to_resume(
    db: AsyncSession,
    contour: Contour,
    version_id: uuid.UUID,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
    sentinel_permit: Permit,
) -> None:
    """The keyset cursor the worker drains with. A batch reports how many
    CANDIDATES it saw — not how many it acted on — because that is the only
    number that can say "the queue is drained", and `last_id` is where the next
    transaction resumes. Without both, the sweep either stops early or holds
    every swept permit's row lock in one transaction (review, Important 3)."""
    from app.modules.permits import jobs

    permits = await _expiring(db, contour, version_id, leshoz, grazing_activity_id, 3)
    ordered = sorted(permits, key=lambda p: p.id)
    assert sentinel_permit.id < ordered[0].id, "uuid7 ids must be time-ordered for the cursor"

    first = await jobs.expire_permits(db, limit=2, after_id=sentinel_permit.id)
    assert (first.scanned, first.processed, first.failed) == (2, 2, 0)
    assert first.last_id == ordered[1].id

    second = await jobs.expire_permits(db, limit=2, after_id=first.last_id)
    assert (second.scanned, second.processed) == (1, 1), "a short batch means drained"
    assert second.last_id == ordered[2].id

    for permit in permits:
        await db.refresh(permit)
    assert {p.status for p in permits} == {"expired"}
    await db.refresh(sentinel_permit)
    assert sentinel_permit.status == "pending_signatures"


async def test_one_bad_row_does_not_cost_its_batch(
    db: AsyncSession,
    contour: Contour,
    version_id: uuid.UUID,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
    sentinel_permit: Permit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each row runs inside its own SAVEPOINT. A bare failure would poison the
    transaction for every row after it (lesson: recovering from a failed
    statement to keep writing on the same session needs a SAVEPOINT), so one
    permit with, say, no resolvable recipient would cost the whole night's
    sweep. Here it costs itself: it stays `active` and is a candidate again
    tomorrow, its neighbours expire, and the batch reports it."""
    from app.modules.permits import jobs, service

    permits = sorted(
        await _expiring(db, contour, version_id, leshoz, grazing_activity_id, 3),
        key=lambda p: p.id,
    )
    doomed = permits[1]
    # `doomed.id`, read now. The stub below runs AFTER the failing row's SAVEPOINT
    # has rolled back and expired that instance, so a `doomed.id` inside it would
    # be a lazy reload from a sync context — the next permit would then fail with
    # `MissingGreenlet` and the test would "prove" the isolation it broke.
    doomed_id = doomed.id
    real = service._holder_recipient

    async def flaky(session: AsyncSession, permit: Permit) -> uuid.UUID:
        if permit.id == doomed_id:
            raise RuntimeError("no recipient for this permit")
        return await real(session, permit)

    monkeypatch.setattr(service, "_holder_recipient", flaky)

    batch = await jobs.expire_permits(db, after_id=sentinel_permit.id)
    assert (batch.scanned, batch.processed, batch.failed) == (3, 2, 1)

    for permit in permits:
        await db.refresh(permit)
    assert doomed.status == "active", "the bad row rolled back alone"
    assert [p.status for p in permits if p.id != doomed.id] == ["expired", "expired"]
    assert await _expired_history_count(db, doomed) == 0


async def test_the_closure_is_audited_as_a_job(db: AsyncSession, expired_permit: Permit) -> None:
    """The same shape expiry's audit test pins, for the sweep that had none:
    `user_id=None` and `correlation_id="job:<uuid>"` (CLAUDE.md). The audit row
    hangs off the PERMIT, while `set_status` writes its own against the
    application — two objects, two trails, one correlation id."""
    from app.modules.audit.models import AuditLog
    from app.modules.permits import jobs, service

    await jobs.close_finished(db)
    row = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.object_id == expired_permit.id,
                AuditLog.action == service.PERMIT_CLOSE_APPLICATION,
            )
        )
    ).scalar_one()
    assert row.user_id is None
    assert row.correlation_id is not None and row.correlation_id.startswith("job:")
    assert row.new_value == {"application_status": "CLOSED"}


async def test_the_closure_sweep_writes_each_row_exactly_once(
    db: AsyncSession, expired_permit: Permit
) -> None:
    """Idempotence, counted rather than inferred from the end status: the second
    run must add neither an `application_status_history` entry claiming a
    transition that never happened nor a second `permit.close_application`."""
    from app.modules.audit.models import AuditLog
    from app.modules.permits import jobs, service

    await jobs.close_finished(db)
    await jobs.close_finished(db)

    history = await db.scalar(
        select(func.count())
        .select_from(ApplicationStatusHistory)
        .where(
            ApplicationStatusHistory.application_id == expired_permit.application_id,
            ApplicationStatusHistory.to_status == "CLOSED",
        )
    )
    audits = await db.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.object_id == expired_permit.id,
            AuditLog.action == service.PERMIT_CLOSE_APPLICATION,
        )
    )
    assert (history, audits) == (1, 1)
