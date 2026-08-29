"""Only the PG advisory-lock contract; APScheduler itself is not re-tested."""

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
