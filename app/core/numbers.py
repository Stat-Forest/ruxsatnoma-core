"""Public numbers `{PREFIX}-{YEAR}-{NUMBER(6)}` (design/03 §"Public numbers").

The counter is bumped inside the CALLER'S transaction under a row lock (plan
03.9a ruling 5а): a submission that fails afterwards rolls the counter back with
it, so a year's numbering has no holes. This is a permit-issuing system for a
government agency — a gap is a question an auditor asks. The contention is
bounded: the lock is held for one short transaction, and a leshoz files
applications in the dozens per day."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import NumberCounter


async def next_public_number(db: AsyncSession, prefix: str, on_date: date) -> str:
    scope = f"{prefix}:{on_date.year}"
    # Create the row if this is the year's first number, then lock it. The
    # ON CONFLICT DO NOTHING makes two concurrent first-submissions safe: one
    # inserts, the other finds it and blocks on the lock below.
    await db.execute(insert(NumberCounter).values(scope=scope).on_conflict_do_nothing())
    row = await db.scalar(
        select(NumberCounter).where(NumberCounter.scope == scope).with_for_update()
    )
    assert row is not None  # the insert above guarantees it
    row.last_value += 1
    return f"{prefix}-{on_date.year}-{row.last_value:06d}"
