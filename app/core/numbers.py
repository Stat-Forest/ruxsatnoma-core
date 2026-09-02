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
    """Allocate the next number in `{prefix}:{on_date.year}` and return it
    formatted. Call inside the business transaction, never in one of its own.

    `on_date` MUST come from `app.core.time.business_today()` — never
    `date.today()` (final review I3; the same rule `auth.deps` and
    `auth.service` state for their own date gates, and `app/core/time.py`
    explains). It selects the counter's SCOPE, so a server-zone date splits
    the series at the wrong instant: between 19:00 UTC on 31 December and
    midnight UTC, Asia/Tashkent is already 1 January, and a number minted from
    `date.today()` on a UTC container continues the OLD year's series for
    invoices whose business date is the new one. design/03 requires the
    numbering be continuous *within a year*, which is the one property ruling
    5а chose a row lock over a sequence to preserve, and the first thing an
    auditor checks.

    The parameter exists so tests can pin a date (they use fixed, randomised
    years); it is not an invitation for the caller to pick one.
    """
    scope = f"{prefix}:{on_date.year}"
    # Create the row if this is the year's first number, then lock it. The
    # ON CONFLICT DO NOTHING makes two concurrent first-submissions safe: one
    # inserts, the other finds it and blocks on the lock below.
    await db.execute(insert(NumberCounter).values(scope=scope).on_conflict_do_nothing())
    # populate_existing: `with_for_update` takes the lock but does NOT refresh an
    # instance this session already holds — the loader populates only unloaded
    # attributes, and `expire_on_commit=False` (app/db.py) never expires them
    # (final review C2, the same trap `app/core/idempotency.py` documents). Every
    # session in the system is per-request or per-worker-iteration today, so no
    # session outlives a counter row; a long-lived one would otherwise increment a
    # stale `last_value` and hand out a DUPLICATE public number under the lock that
    # exists to prevent exactly that.
    row = await db.scalar(
        select(NumberCounter)
        .where(NumberCounter.scope == scope)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert row is not None  # the insert above guarantees it
    row.last_value += 1
    return f"{prefix}-{on_date.year}-{row.last_value:06d}"
