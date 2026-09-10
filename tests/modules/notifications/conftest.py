"""Notification API tests drive a real app (create_app + lifespan) against the test DB."""

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def inapp_params(db, *, object_id, event_code: str) -> dict:
    """The `params` of the ONE in-app row `notify()` wrote for `object_id` under
    `event_code` — what the cabinet inbox renders its status chips from. Exactly
    one row is asserted: a second one would mean the flow notified twice.
    """
    from sqlalchemy import select

    from app.modules.notifications.models import Notification

    rows = (
        await db.scalars(
            select(Notification).where(
                Notification.object_id == object_id,
                Notification.event_code == event_code,
                Notification.channel == "inapp",
            )
        )
    ).all()
    assert len(rows) == 1, [row.event_code for row in rows]
    return rows[0].params
