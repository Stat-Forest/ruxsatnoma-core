"""E-mail delivery seam (SMTP). The real implementation is added in Task 6 of plan
03.5 and switched on by `email_mode=real`."""

from functools import lru_cache
from typing import Protocol

import structlog

from app.config import get_settings

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


@lru_cache(maxsize=1)
def get_email_sender() -> EmailSender:
    if get_settings().email_mode == "mock":
        return MockEmailSender()
    raise NotImplementedError("real SMTP adapter arrives in Task 6")
