"""Purge/expiry jobs: retention deletes and the representation-expiry flip+audit."""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.db import make_session_factory
from app.modules.auth.models import Applicant
from app.workers.jobs import (
    expire_invoices,
    expire_representations,
    oversight_sweep,
    purge_stale_rows,
    refund_sla_sweep,
    sla_sweep,
)
from tests.modules.auth.test_sessions import make_user


async def test_purge_deletes_only_stale_rows(engine, db):
    # committed rows outlive this test (a separate session does the purge, so
    # setup must be visible to it) — unique hashes keep reruns against the same
    # long-lived dev Postgres from colliding with a prior run's survivor.
    old_hash = f"h-old-{uuid.uuid4().hex[:8]}"
    fresh_hash = f"h-fresh-{uuid.uuid4().hex[:8]}"
    old = datetime.now(UTC) - timedelta(days=60)
    fresh = datetime.now(UTC) + timedelta(minutes=5)
    await db.execute(
        text(
            "INSERT INTO otp_codes (id, code_hash, purpose, expires_at, attempts) VALUES "
            "(gen_random_uuid(), :old_hash, 'phone_verify', :old, 0),"
            "(gen_random_uuid(), :fresh_hash, 'phone_verify', :fresh, 0)"
        ),
        {"old_hash": old_hash, "fresh_hash": fresh_hash, "old": old, "fresh": fresh},
    )
    delivered_old = datetime.now(UTC) - timedelta(days=30)
    await db.execute(
        text(
            # attempts has no DB-level default (only the ORM-side default=0 in the
            # model, migration 0008) — a raw INSERT that omits it violates NOT NULL.
            "INSERT INTO outbox_messages (id, destination, payload, status, attempts, delivered_at)"
            " VALUES "
            "(gen_random_uuid(), 'x', '{}', 'delivered', 0, :d),"
            "(gen_random_uuid(), 'x', '{}', 'dead', 0, NULL)"  # dead rows are the DLQ — kept
        ),
        {"d": delivered_old},
    )
    await db.commit()

    counts = await purge_stale_rows(make_session_factory(engine))
    assert counts["otp_codes"] >= 1
    assert counts["outbox_messages"] >= 1

    left = (
        await db.execute(
            text("SELECT count(*) FROM outbox_messages WHERE status = 'dead' AND destination = 'x'")
        )
    ).scalar_one()
    assert left >= 1  # dead rows survived
    fresh_left = (
        await db.execute(
            text("SELECT count(*) FROM otp_codes WHERE code_hash = :h"), {"h": fresh_hash}
        )
    ).scalar_one()
    assert fresh_left == 1

    audited = (
        await db.execute(text("SELECT count(*) FROM audit_log WHERE action = 'purge.run'"))
    ).scalar_one()
    assert audited >= 1


def _unique_stir() -> str:
    # Same shape as tests/modules/auth/test_legal_applicants.py's unique_stir():
    # a fresh 9-digit stir per call so reruns against the shared dev Postgres
    # never collide on applicants.uq_applicants_stir.
    return f"9{uuid.uuid4().int % 10**8:08d}"


async def test_expire_representations_flips_and_audits(engine, db):
    # No make_applicant fixture exists at this level (checked tests/modules/auth/);
    # inline the applicant the way tests/modules/auth/test_applicant_models.py does,
    # and the representation row via raw SQL like the purge test above.
    user = await make_user(db)
    applicant = Applicant(kind="legal", stir=_unique_stir(), name="OOO Test")
    db.add(applicant)
    await db.flush()
    await db.execute(
        text(
            "INSERT INTO representations (id, applicant_id, user_id, basis, valid_from,"
            " valid_until, status) VALUES"
            " (gen_random_uuid(), :a, :u, 'director_registry', :vf, :vu, 'active')"
        ),
        {"a": applicant.id, "u": user.id, "vf": date(2026, 1, 1), "vu": date(2026, 1, 2)},
    )
    await db.commit()

    n = await expire_representations(make_session_factory(engine))
    assert n >= 1
    status = (
        await db.execute(
            text("SELECT status FROM representations WHERE user_id = :u"), {"u": user.id}
        )
    ).scalar_one()
    assert status == "expired"
    audited = (
        await db.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'representation.expire'")
        )
    ).scalar_one()
    assert audited >= 1


# --- 3.11a task 7: the permit sweeps drain in batches -------------------------


class _StubSession:
    """Just enough session for `_drain_batches`: an async context manager that
    records its commits. No database — the loop under test is pure control flow
    (advance the cursor, stop on a short batch, commit between batches), and the
    sweeps' own SQL is covered against real rows in
    tests/modules/permits/test_jobs.py."""

    def __init__(self, commits: list[int]) -> None:
        self._commits = commits

    async def __aenter__(self) -> _StubSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def commit(self) -> None:
        self._commits.append(1)


def _stub_factory(commits: list[int]) -> async_sessionmaker[AsyncSession]:
    """`_drain_batches` only ever calls its factory and `async with`es the
    result, so a callable returning `_StubSession` satisfies it at runtime; the
    cast is what tells pyright that, since `async_sessionmaker` is a concrete
    class rather than a protocol."""
    return cast("async_sessionmaker[AsyncSession]", lambda: _StubSession(commits))


async def test_a_permit_sweep_commits_every_batch_and_resumes_where_it_stopped():
    """Review, Important 3. The whole point of batching is that the transaction
    ends between batches — an unbounded sweep held a `FOR UPDATE` lock on every
    candidate until the night was over, and one raising row rolled all of it
    back. The loop must therefore commit per batch, carry `last_id` forward as
    the cursor, and keep going until a SHORT batch says the queue is drained —
    `processed` cannot be the stop condition, since the closure sweep skips
    candidates whose application is already CLOSED."""
    from app.modules.permits import jobs as permits_jobs
    from app.workers.jobs import _drain_batches

    full = permits_jobs.BATCH_SIZE
    ids = [uuid.uuid4() for _ in range(3)]
    batches = [
        permits_jobs.SweepBatch(scanned=full, processed=2, failed=0, last_id=ids[0]),
        permits_jobs.SweepBatch(scanned=full, processed=3, failed=1, last_id=ids[1]),
        permits_jobs.SweepBatch(scanned=1, processed=1, failed=0, last_id=ids[2]),
    ]
    cursors: list[uuid.UUID | None] = []
    commits: list[int] = []

    async def sweep(db, after_id):
        cursors.append(after_id)
        return batches[len(cursors) - 1]

    total = await _drain_batches(_stub_factory(commits), sweep, name="stub_sweep")

    assert total == 6, "every batch's processed rows count, the failed one does not"
    assert cursors == [None, ids[0], ids[1]], "each batch resumes after the last one"
    assert len(commits) == 3, "one transaction per batch, not one for the night"


async def test_an_empty_permit_sweep_opens_one_transaction_and_stops():
    """The ordinary night: nothing expired, so the first batch is short and the
    loop must not ask again."""
    from app.modules.permits import jobs as permits_jobs
    from app.workers.jobs import _drain_batches

    commits: list[int] = []
    calls = 0

    async def sweep(db, after_id):
        nonlocal calls
        calls += 1
        return permits_jobs.SweepBatch(scanned=0, processed=0, failed=0, last_id=None)

    assert await _drain_batches(_stub_factory(commits), sweep, name="stub_sweep") == 0
    assert (calls, len(commits)) == (1, 1)


async def test_an_empty_permit_sweep_still_logs_its_zero():
    """F24: `_drain_batches`' own `elif total:` used to mean a quiet night and
    a scheduler that never started produced the identical trail — nothing.
    Fixed to `else:`, so `processed=0` is logged unconditionally at
    completion, the same as the `if counts[...]:` jobs below."""
    from app.modules.permits import jobs as permits_jobs
    from app.workers.jobs import _drain_batches

    commits: list[int] = []

    async def sweep(db, after_id):
        return permits_jobs.SweepBatch(scanned=0, processed=0, failed=0, last_id=None)

    with capture_logs() as logs:
        total = await _drain_batches(_stub_factory(commits), sweep, name="stub_sweep")

    assert total == 0
    entries = [entry for entry in logs if entry.get("event") == "job.stub_sweep"]
    assert len(entries) == 1
    assert entries[0]["processed"] == 0
    assert entries[0]["log_level"] == "info"


# --- F24: every nightly sweep logs its result, zeros included ----------------
#
# Each wrapper's own inner sweep is stubbed to return an all-zero count,
# rather than relied on to BE zero: this file's own database is the shared,
# persistent one (`../CLAUDE.md`), and another test's leftover due-soon
# invoice or SLA-eligible application would make a real "quiet night" run
# unreliable to assert on. What is under test here is the WRAPPER's own
# `if counts[...]:` (now unconditional) — never the sweep query itself,
# which each module's own test suite already covers.


async def test_expire_invoices_logs_a_quiet_night(engine, monkeypatch):
    from app.workers import jobs as workers_jobs

    async def fake_sweep(db):
        return {"expired": 0, "reminded": 0}

    monkeypatch.setattr(workers_jobs.payments_jobs, "expiry_sweep", fake_sweep)

    with capture_logs() as logs:
        counts = await expire_invoices(make_session_factory(engine))

    assert counts == {"expired": 0, "reminded": 0}
    entries = [entry for entry in logs if entry.get("event") == "job.expire_invoices"]
    assert len(entries) == 1
    assert entries[0]["expired"] == 0
    assert entries[0]["reminded"] == 0


async def test_refund_sla_sweep_logs_a_quiet_night(engine, monkeypatch):
    from app.workers import jobs as workers_jobs

    async def fake_sweep(db):
        return {"flagged": 0}

    monkeypatch.setattr(workers_jobs.payments_jobs, "refund_sla_sweep", fake_sweep)

    with capture_logs() as logs:
        counts = await refund_sla_sweep(make_session_factory(engine))

    assert counts == {"flagged": 0}
    entries = [entry for entry in logs if entry.get("event") == "job.refund_sla_sweep"]
    assert len(entries) == 1
    assert entries[0]["flagged"] == 0


async def test_applications_sla_sweep_logs_a_quiet_night(engine, monkeypatch):
    from app.workers import jobs as workers_jobs

    async def fake_sweep(db):
        return {"reminded": 0, "flagged": 0}

    monkeypatch.setattr(workers_jobs.applications_jobs, "sla_sweep", fake_sweep)

    with capture_logs() as logs:
        counts = await sla_sweep(make_session_factory(engine))

    assert counts == {"reminded": 0, "flagged": 0}
    entries = [entry for entry in logs if entry.get("event") == "job.applications_sla_sweep"]
    assert len(entries) == 1
    assert entries[0]["reminded"] == 0
    assert entries[0]["flagged"] == 0


async def test_oversight_sweep_logs_a_quiet_run(engine, monkeypatch):
    from app.workers import jobs as workers_jobs

    async def fake_sweep(db, *, correlation_id):
        return {"harvested": 0, "overlaps_raised": 0, "long_active_raised": 0}

    monkeypatch.setattr(workers_jobs.oversight_jobs, "sweep", fake_sweep)

    with capture_logs() as logs:
        counts = await oversight_sweep(make_session_factory(engine))

    assert counts == {"harvested": 0, "overlaps_raised": 0, "long_active_raised": 0}
    entries = [entry for entry in logs if entry.get("event") == "job.oversight_sweep"]
    assert len(entries) == 1
    assert entries[0]["harvested"] == 0
    assert entries[0]["overlaps_raised"] == 0
    assert entries[0]["long_active_raised"] == 0
