"""SMTP sending (plan 03.5 ruling 3) — aiosmtplib is stubbed; no mail leaves the test."""

import pytest

from app.config import Settings
from app.modules.integrations.adapters.email import SmtpEmailSender

SETTINGS = Settings(
    email_mode="real",
    smtp_host="smtp.test",
    smtp_port=587,
    smtp_user="robot@example.com",
    smtp_password="p",
    smtp_from="Ruxsatnoma <robot@example.com>",
)


@pytest.fixture
def captured(monkeypatch):
    sent: list[dict] = []

    async def _send(message, **kwargs):
        sent.append({"message": message, "kwargs": kwargs})

    monkeypatch.setattr("aiosmtplib.send", _send)
    return sent


async def test_sends_a_plain_text_message_with_the_configured_sender(captured):
    message_id = await SmtpEmailSender(SETTINGS).send(
        to="user@example.com", subject="Рухсатнома", text="Матн"
    )
    assert len(captured) == 1
    message = captured[0]["message"]
    assert message["To"] == "user@example.com"
    assert message["From"] == "Ruxsatnoma <robot@example.com>"
    assert message["Subject"] == "Рухсатнома"
    assert message.get_content().strip() == "Матн"
    assert message_id == message["Message-ID"]
    assert captured[0]["kwargs"]["hostname"] == "smtp.test"
    assert captured[0]["kwargs"]["port"] == 587
    assert captured[0]["kwargs"]["start_tls"] == SETTINGS.smtp_starttls
    assert captured[0]["kwargs"]["timeout"] == SmtpEmailSender.TIMEOUT_SECONDS


async def test_an_empty_subject_gets_the_default_one(captured):
    await SmtpEmailSender(SETTINGS).send(to="user@example.com", subject="", text="Матн")
    assert captured[0]["message"]["Subject"] == "Ruxsatnoma"


async def test_credentials_are_passed_but_never_returned(captured):
    result = await SmtpEmailSender(SETTINGS).send(to="u@e.com", subject="s", text="t")
    assert captured[0]["kwargs"]["username"] == "robot@example.com"
    assert captured[0]["kwargs"]["password"] == "p"
    # The return value is an RFC 5322 message id, never the credential.
    assert result is not None
    assert result.startswith("<")


async def test_email_mode_real_requires_host_and_from():
    with pytest.raises(ValueError):
        Settings(app_env="dev", email_mode="real", smtp_host="", smtp_from="")


async def test_prod_forbids_the_email_mock():
    with pytest.raises(ValueError):
        Settings(
            app_env="prod",
            secret_key="x",
            s3_secret_key="y",
            oneid_mode="real",
            eimzo_mode="real",
            sms_mode="real",
            email_mode="mock",
            eskiz_email="a",
            eskiz_password="b",
            eskiz_sender="c",
            eskiz_callback_secret="d",
        )


async def test_real_mode_returns_the_smtp_sender(monkeypatch):
    """Mirrors test_sms_adapter.test_real_mode_returns_the_eskiz_sender: the
    factory-selection logic itself must be under test, not just the sender class
    constructed directly (review finding, plan 03.5 Task 6)."""
    from app.config import get_settings
    from app.modules.integrations.adapters import email

    monkeypatch.setenv("EMAIL_MODE", "real")
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_FROM", "Ruxsatnoma <robot@example.com>")
    get_settings.cache_clear()
    email.get_email_sender.cache_clear()
    try:
        assert isinstance(email.get_email_sender(), email.SmtpEmailSender)
    finally:
        get_settings.cache_clear()
        email.get_email_sender.cache_clear()


def test_mask_email():
    """The success log must show only the local part's first character and the
    full domain (review finding: the log line used to echo `to` unmasked)."""
    from app.modules.integrations.adapters.email import _mask_email

    assert _mask_email("user@example.com") == "u***@example.com"


def test_mask_email_without_an_at_sign_never_echoes_the_input():
    """Defense in depth, mirrors auth._mask_target's degenerate branch: must
    degrade safely rather than echo the original value back."""
    from app.modules.integrations.adapters.email import _mask_email

    assert _mask_email("not-an-email") == "***"
