"""business_today() must follow Asia/Tashkent, not the server's own zone (finding 11:
`date.today()` at the admin module's three call sites drifts for ~5h/day on UTC)."""

from datetime import UTC, date, datetime
from unittest.mock import patch

from app.core.time import TASHKENT, business_today


def test_business_today_matches_a_fresh_tashkent_now():
    assert business_today() == datetime.now(TASHKENT).date()


def test_business_today_follows_tashkent_when_it_disagrees_with_utc():
    """01:00 in Tashkent (UTC+5) on 2026-01-01 is still 2025-12-31 in UTC — exactly
    the window where a server-local/UTC `date.today()` would be wrong by one day."""
    tashkent_instant = datetime(2026, 1, 1, 1, 0, tzinfo=TASHKENT)
    assert tashkent_instant.astimezone(UTC).date() == date(2025, 12, 31)

    with patch("app.core.time.datetime") as mock_datetime:
        mock_datetime.now.return_value = tashkent_instant
        assert business_today() == date(2026, 1, 1)
    mock_datetime.now.assert_called_once_with(TASHKENT)
