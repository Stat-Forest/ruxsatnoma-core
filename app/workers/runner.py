"""Standalone worker entry (plan 03.4 ruling 2): the same components main.py
embeds, run as their own process when the deployment separates them."""

import asyncio
import signal

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import get_settings
from app.core.logging import configure_logging
from app.db import make_engine, make_session_factory
from app.event_subscriptions import register_event_subscriptions
from app.workers.outbox import run_outbox_loop
from app.workers.scheduler import run_scheduler

logger = structlog.get_logger(__name__)


async def run_all(
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    *,
    stop: asyncio.Event,
    only: str | None = None,
) -> None:
    tasks: list[asyncio.Task[None]] = []
    if only in (None, "outbox"):
        tasks.append(asyncio.create_task(run_outbox_loop(factory, stop=stop)))
    if only in (None, "scheduler"):
        tasks.append(asyncio.create_task(run_scheduler(engine, factory, stop=stop)))
    # return_exceptions=True (review finding 2): plain gather() would propagate
    # the FIRST task's exception immediately and return without awaiting the
    # other one, leaving it running orphaned (nothing left to await/cancel it).
    # Both loops already swallow their own routine failures internally, so a
    # result here means something unexpected escaped one of them — log it but
    # let the sibling run to completion (each worker's failure domain is its
    # own, matching how the outbox loop and the scheduler already behave).
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            logger.error("worker.run_all.task_failed", error=repr(result))


def main(only: str | None = None) -> None:
    settings = get_settings()
    configure_logging(settings.log_format)
    # A standalone process never calls app.main.create_app(), so nothing else
    # wires up the bus's subscribers here — without this, a handler a future
    # stage adds (3.10a's invoice issuer, 3.11's permit issuer) would silently
    # never run for an event published from this process (a scheduled job in
    # app/workers/jobs.py, the outbox loop), even though the embedded
    # deployment (workers_mode=embedded) fires it correctly, since that path
    # goes through create_app() first (review round 1, finding I2).
    register_event_subscriptions()

    async def _run() -> None:
        engine = make_engine(settings.database_url)
        factory = make_session_factory(engine)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        try:
            await run_all(engine, factory, stop=stop, only=only)
        finally:
            await engine.dispose()

    asyncio.run(_run())
