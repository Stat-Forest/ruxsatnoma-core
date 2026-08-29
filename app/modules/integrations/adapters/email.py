"""E-mail delivery seam (SMTP). The real implementation is added in Task 6 of plan
03.5 and switched on by `email_mode=real`."""

from email.message import EmailMessage
from email.utils import make_msgid
from functools import lru_cache
from typing import Protocol

import aiosmtplib
import structlog

from app.config import Settings, get_settings

logger = structlog.get_logger(__name__)


class EmailSender(Protocol):
    async def send(self, *, to: str, subject: str, text: str) -> str | None: ...


class MockEmailSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, *, to: str, subject: str, text: str) -> str | None:
        self.sent.append((to, subject, text))
        logger.info("email.mock_send", to=to, subject=subject)
        return None


DEFAULT_SUBJECT = "Ruxsatnoma"


class EmailError(Exception):
    """Delivery failure carrying no recipient content (see EskizError)."""


class SmtpEmailSender:
    """Plain-text SMTP over aiosmtplib (decision #26: no sync libraries).

    A fresh connection per message: volume is low, and a pooled connection held
    across an idle night is closed by most relays anyway.
    """

    TIMEOUT_SECONDS = 10.0

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, *, to: str, subject: str, text: str) -> str | None:
        message = EmailMessage()
        message["From"] = self._settings.smtp_from
        message["To"] = to
        message["Subject"] = subject or DEFAULT_SUBJECT
        message["Message-ID"] = make_msgid()
        message.set_content(text)
        try:
            await aiosmtplib.send(
                message,
                hostname=self._settings.smtp_host,
                port=self._settings.smtp_port,
                username=self._settings.smtp_user or None,
                password=self._settings.smtp_password or None,
                start_tls=self._settings.smtp_starttls,
                timeout=self.TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # Same rule as the SMS sender: the body never reaches last_error.
            raise EmailError(f"smtp send failed: {type(exc).__name__}") from None
        logger.info("email.smtp_send", to=_mask_email(to))
        return message["Message-ID"]


def _mask_email(address: str) -> str:
    """E-mail addresses are personal data; log the local part's first character
    and the full domain only (auth._mask_target idiom, mirrored here — integrations
    is level 0 and cannot import from auth — same reasoning as sms._mask)."""
    local, _, host = address.partition("@")
    return f"{local[:1]}***@{host}" if host else "***"


@lru_cache(maxsize=1)
def get_email_sender() -> EmailSender:
    settings = get_settings()
    if settings.email_mode == "mock":
        return MockEmailSender()
    return SmtpEmailSender(settings)
