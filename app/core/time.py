"""Business-day helper: the project stores UTC but reasons/displays in Asia/Tashkent
(backend/CLAUDE.md "Time" rule). Plain `date.today()` follows the SERVER's local
zone, which on a UTC container means it reports *yesterday* for roughly five hours a
day (19:00-24:00 Tashkent time) — wrong for anything that gates on "today", such as
classifier validity windows (admin.service.archive_classifier_item,
admin.repo.list_classifier_items) and, later, seasons/rotations/permit validity.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

TASHKENT = ZoneInfo("Asia/Tashkent")


def business_today() -> date:
    """Today's calendar date in Asia/Tashkent, independent of the server's own zone."""
    return datetime.now(TASHKENT).date()
