"""Outbox delivery loop: drain due rows one at a time, sleep when idle."""

import asyncio
import contextlib

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations import service

logger = structlog.get_logger(__name__)


async def run_outbox_loop(
    factory: async_sessionmaker[AsyncSession], *, stop: asyncio.Event, poll_seconds: float = 5.0
) -> None:
    logger.info("worker.outbox.start")
    while not stop.is_set():
        try:
            async with factory() as db:
                delivered = await service.deliver_one(db)
        except Exception:
            logger.exception("worker.outbox.error")
            delivered = False
        if not delivered:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
    logger.info("worker.outbox.stop")


if __name__ == "__main__":
    from app.workers.runner import main

    main(only="outbox")
