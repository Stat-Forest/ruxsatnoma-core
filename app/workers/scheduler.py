"""Periodic-job scheduler (decision #36: APScheduler, no Redis). Exactly one
instance in the cluster is active: a session-level PG advisory lock decides;
losers stand by and retry."""

import asyncio
import contextlib
from datetime import datetime
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.workers import jobs

logger = structlog.get_logger(__name__)

ADVISORY_LOCK = (37701, 1)  # arbitrary fixed pair; project-unique
TIMEZONE = "Asia/Tashkent"
_HEARTBEAT_SECONDS = 60.0


def _wrap(factory: async_sessionmaker[AsyncSession], fn: Any) -> Any:
    """Job wrapper: a failing job logs and never kills the scheduler."""

    async def run() -> None:
        try:
            await fn(factory)
        except Exception:
            logger.exception("job.error", job=fn.__name__)

    run.__name__ = fn.__name__
    return run


def build_scheduler(factory: async_sessionmaker[AsyncSession]) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=TIMEZONE)
    # next_run_time=now → also run once at startup (covers missed windows).
    now = datetime.now(sched.timezone)
    sched.add_job(
        _wrap(factory, jobs.expire_representations),
        CronTrigger(hour=0, minute=5, timezone=TIMEZONE),
        next_run_time=now,
        id="expire_representations",
        misfire_grace_time=3600,
        coalesce=True,
    )
    sched.add_job(
        _wrap(factory, jobs.purge_stale_rows),
        CronTrigger(hour=0, minute=15, timezone=TIMEZONE),
        next_run_time=now,
        id="purge_stale_rows",
        misfire_grace_time=3600,
        coalesce=True,
    )
    sched.add_job(
        _wrap(factory, jobs.expire_invoices),
        CronTrigger(hour=0, minute=25, timezone=TIMEZONE),
        next_run_time=now,
        id="expire_invoices",
        misfire_grace_time=3600,
        coalesce=True,
    )
    sched.add_job(
        _wrap(factory, jobs.alert_dead_outbox),
        IntervalTrigger(minutes=5, timezone=TIMEZONE),
        next_run_time=now,
        id="alert_dead_outbox",
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Every 10 seconds: an operator who just uploaded a leshoz should not wait
    # a minute for anything to start happening (plan 03.6a ruling 6). Cheap
    # when idle — one indexed SELECT ... FOR UPDATE SKIP LOCKED against
    # `ix_gis_imports_pending`.
    sched.add_job(
        _wrap(factory, jobs.process_gis_imports),
        IntervalTrigger(seconds=10, timezone=TIMEZONE),
        next_run_time=now,
        id="process_gis_imports",
        misfire_grace_time=3600,
        coalesce=True,
    )
    return sched


async def run_scheduler(
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    *,
    stop: asyncio.Event,
    retry_seconds: float = 30.0,
) -> None:
    """Hold the advisory lock on a dedicated connection; run APScheduler while
    it lives. Lost connection → scheduler stops, loop re-competes for the lock.

    Any failure here — including one before the lock is even acquired (e.g.
    Postgres not reachable yet) — falls into the same retry sleep instead of
    killing the task (review finding 2): a standby that can never connect must
    keep retrying, not exit silently and leave jobs unscheduled forever.
    """
    c, o = ADVISORY_LOCK
    logger.info("worker.scheduler.start")
    while not stop.is_set():
        try:
            async with engine.connect() as conn:
                # AUTOCOMMIT: pg_try_advisory_lock and the heartbeat below are
                # session-level, not transactional — without this the connection
                # sits "idle in transaction" for as long as this process holds
                # the lock (review finding 1), pinning xmin and blocking vacuum.
                await conn.execution_options(isolation_level="AUTOCOMMIT")
                got = (
                    await conn.execute(
                        text("SELECT pg_try_advisory_lock(:c, :o)"), {"c": c, "o": o}
                    )
                ).scalar()
                if not got:
                    logger.debug("worker.scheduler.standby")
                else:
                    sched = build_scheduler(factory)
                    sched.start()
                    logger.info("worker.scheduler.active")
                    try:
                        while not stop.is_set():
                            with contextlib.suppress(TimeoutError):
                                await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_SECONDS)
                            if stop.is_set():
                                break
                            await conn.execute(text("SELECT 1"))  # dead conn → lock is gone
                    except Exception:
                        logger.exception("worker.scheduler.lost_lock")
                    finally:
                        sched.shutdown(wait=False)
                        # A checkin (plain `async with` exit) returns this connection
                        # to the pool WITHOUT releasing the session-level advisory
                        # lock (review finding 1) — an idle pooled connection would
                        # then wedge every future election, cluster-wide, forever
                        # (pool_pre_ping keeps pinging it "healthy"). invalidate()
                        # physically closes it instead, which Postgres always
                        # recognizes as the session ending, lock included.
                        await conn.invalidate()
        except Exception:
            logger.exception("worker.scheduler.connect_error")
        if not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=retry_seconds)
    logger.info("worker.scheduler.stop")


if __name__ == "__main__":
    from app.workers.runner import main

    main(only="scheduler")
