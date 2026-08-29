"""Notification API tests drive a real app (create_app + lifespan) against the test DB."""

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
