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
