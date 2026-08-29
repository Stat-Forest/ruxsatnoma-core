"""Auth API tests drive a real app (create_app + lifespan): point it at the test DB."""

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()  # do not leak the test URL into non-auth tests


@pytest.fixture(autouse=True)
def _clear_sent_codes():
    from app.modules.integrations.adapters.otp_sender import MockOtpSender, get_otp_sender

    sender = get_otp_sender()
    if isinstance(sender, MockOtpSender):
        sender.sent.clear()
    yield
