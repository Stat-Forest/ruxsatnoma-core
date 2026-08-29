"""Outbox loop: delivers a due row via the sender registry, then stops on signal."""

import asyncio
import subprocess
import sys
from pathlib import Path

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


def test_a_standalone_worker_process_registers_the_notification_senders_on_import():
    """The registration comment in app/workers/outbox.py ('main.py gets them
    transitively through the routers; a standalone worker process has no routers')
    is only true if importing outbox.py alone, with nothing upstream of it, ends up
    with 'sms' and 'email' in the registry. Every other test in this suite gets
    there for free — the test session always imports notifications.service via some
    router first — so only a fresh interpreter that imports app.workers.outbox and
    nothing else can prove the standalone-worker path actually works. Remove the
    `import app.modules.notifications.service` line in outbox.py to watch this fail:
    the subprocess registers only 'sms_otp' (from integrations.service) and dies on
    the 'sms' assertion.
    """
    backend_root = Path(__file__).resolve().parents[2]
    script = (
        "import app.workers.outbox\n"
        "from app.modules.integrations.senders import SENDERS\n"
        "assert 'sms' in SENDERS, SENDERS.keys()\n"
        "assert 'email' in SENDERS, SENDERS.keys()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
