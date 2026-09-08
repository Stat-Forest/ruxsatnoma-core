"""Business-day helper: the project stores UTC but reasons/displays in Asia/Tashkent
(backend/CLAUDE.md "Time" rule). Plain `date.today()` follows the SERVER's local
zone, which on a UTC container means it reports *yesterday* for roughly five hours a
day (19:00-24:00 Tashkent time) — wrong for anything that gates on "today", such as
classifier validity windows (admin.service.archive_classifier_item,
admin.repo.list_classifier_items) and, later, seasons/rotations/permit validity.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

TASHKENT = ZoneInfo("Asia/Tashkent")


def business_today() -> date:
    """Today's calendar date in Asia/Tashkent, independent of the server's own zone."""
    return datetime.now(TASHKENT).date()


def in_quiet_hours(start_hour: int, end_hour: int, *, now: datetime | None = None) -> bool:
    """Is it currently inside the nightly SMS quiet window (decision #152)?

    The window is expressed in Asia/Tashkent whole hours and WRAPS midnight —
    `start_hour=21`, `end_hour=8` means 21:00-07:59, which is the only shape this
    is ever used in. A non-wrapping pair (8, 21) is still handled correctly, so an
    operator who inverts the two settings gets a daytime pause rather than a window
    that silently never closes; `start == end` means no quiet hours at all, which
    is how the feature is turned off without a third setting.
    """
    hour = (now or datetime.now(TASHKENT)).astimezone(TASHKENT).hour
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def next_quiet_window_end(end_hour: int, *, now: datetime | None = None) -> datetime:
    """The next Tashkent `end_hour:00`, as UTC — when a message held by the window
    becomes due. Used for logging and for `next_attempt_at`, never for a decision:
    the decision is `in_quiet_hours` at delivery time, so a message enqueued before
    the window and retried inside it is held too."""
    local = (now or datetime.now(TASHKENT)).astimezone(TASHKENT)
    target = local.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(UTC)


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
