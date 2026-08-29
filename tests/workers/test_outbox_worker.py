"""Outbox loop: delivers a due row via the sender registry, then stops on signal."""

import asyncio

from app.db import make_session_factory
from app.modules.integrations import senders, service
from app.modules.integrations.models import OutboxMessage
from app.workers.outbox import run_outbox_loop


async def test_loop_delivers_and_stops(engine, db):
    delivered: list[dict] = []

    async def sink(db, payload: dict) -> None:
        delivered.append(payload)

    senders.SENDERS["_loop_test"] = sink
    try:
        msg = await service.enqueue(db, destination="_loop_test", payload={"k": 1})
        assert msg is not None
        await db.commit()

        stop = asyncio.Event()
        factory = make_session_factory(engine)
        task = asyncio.create_task(run_outbox_loop(factory, stop=stop, poll_seconds=0.05))
        for _ in range(100):  # up to ~2s
            if delivered:
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        assert delivered == [{"k": 1}]
        row = await db.get(OutboxMessage, msg.id)
        assert row is not None
        await db.refresh(row)
        assert row.status == "delivered"
    finally:
        senders.SENDERS.pop("_loop_test", None)
