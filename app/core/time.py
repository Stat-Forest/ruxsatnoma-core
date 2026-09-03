"""Business-day helper: the project stores UTC but reasons/displays in Asia/Tashkent
(backend/CLAUDE.md "Time" rule). Plain `date.today()` follows the SERVER's local
zone, which on a UTC container means it reports *yesterday* for roughly five hours a
day (19:00-24:00 Tashkent time) — wrong for anything that gates on "today", such as
classifier validity windows (admin.service.archive_classifier_item,
admin.repo.list_classifier_items) and, later, seasons/rotations/permit validity.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TASHKENT = ZoneInfo("Asia/Tashkent")


def business_today() -> date:
    """Today's calendar date in Asia/Tashkent, independent of the server's own zone."""
    return datetime.now(TASHKENT).date()


def add_working_days(start: date, days: int) -> date:
    """`start` plus `days` working days, counting **Monday-Friday only and
    skipping no holidays** (payments 3.10b ruling 18; second caller: stage
    3.9b). There is no holiday table anywhere in this system, and this
    function does not invent one — it is acceptable ONLY because every
    caller uses the result as a CONTROL date (a register highlight, an audit
    row such as `payments`' own RI-07), never as the legality of anything a
    citizen or an accountant did on a given day. A caller that needs a
    legally binding deadline must not reach for this.

    `days` counts forward from the day AFTER `start`, the same way a person
    reads "20 working days from today" — `start` itself is never counted."""
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:  # Monday=0 ... Friday=4; Saturday/Sunday skipped
            remaining -= 1
    return current
