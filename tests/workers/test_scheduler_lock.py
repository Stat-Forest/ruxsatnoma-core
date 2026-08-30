"""Only the PG advisory-lock contract; APScheduler itself is not re-tested."""

import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from app.workers.scheduler import ADVISORY_LOCK


async def test_advisory_lock_is_exclusive(engine):
    c, o = ADVISORY_LOCK
    async with engine.connect() as conn1, engine.connect() as conn2:
        got1 = (
            await conn1.execute(text("SELECT pg_try_advisory_lock(:c, :o)"), {"c": c, "o": o})
        ).scalar()
        got2 = (
            await conn2.execute(text("SELECT pg_try_advisory_lock(:c, :o)"), {"c": c, "o": o})
        ).scalar()
        assert got1 is True and got2 is False
        await conn1.execute(text("SELECT pg_advisory_unlock(:c, :o)"), {"c": c, "o": o})


def test_a_standalone_scheduler_process_registers_the_gis_import_job():
    """Sibling of `test_a_standalone_worker_process_registers_the_notification_senders_on_import`
    (tests/workers/test_outbox_worker.py), for the OTHER half of `python -m
    app.workers`. Plan 03.6a ruling 20: with `workers_mode=off` and no worker
    process, `POST /gis/imports` answers 202 and nothing ever happens — the same
    silent stall the outbox lesson describes. This proves the reverse: a fresh
    interpreter importing only `app.workers.scheduler` really does reach
    `jobs.process_gis_imports` and schedule it, with no router upstream to pull
    the gis module in for free (which is how every other test in this suite gets
    there).
    """
    backend_root = Path(__file__).resolve().parents[2]
    script = (
        "from app.workers.scheduler import build_scheduler\n"
        "sched = build_scheduler(None)\n"
        "ids = {job.id for job in sched.get_jobs()}\n"
        "assert 'process_gis_imports' in ids, ids\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
