"""Standalone worker entry (plan 03.4 ruling 2): the same components main.py
embeds, run as their own process when the deployment separates them."""

import asyncio
import signal

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import get_settings
from app.core.logging import configure_logging
from app.db import make_engine, make_session_factory
from app.workers.outbox import run_outbox_loop
from app.workers.scheduler import run_scheduler


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
    await asyncio.gather(*tasks)


def main(only: str | None = None) -> None:
    settings = get_settings()
    configure_logging(settings.log_format)

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
