"""SMS delivery seam. The real provider is Eskiz (design/04 §4); it is added in
Task 5 of plan 03.5 and switched on by `sms_mode=real` at stage 5.4."""

from functools import lru_cache
from typing import Protocol

import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)


class SmsSender(Protocol):
    async def send(self, *, phone: str, text: str, reference: str) -> str | None:
        """Deliver one SMS. `reference` is our own correlation id (the notification
        uuid), echoed back by the provider's delivery report. Returns the provider's
        message id when it supplies one. Raises on failure — the outbox retries."""
        ...


class MockSmsSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, *, phone: str, text: str, reference: str) -> str | None:
        self.sent.append((phone, text, reference))
        logger.info("sms.mock_send", phone=phone, reference=reference)
        return f"mock-{reference}"


@lru_cache(maxsize=1)
def get_sms_sender() -> SmsSender:
    if get_settings().sms_mode == "mock":
        return MockSmsSender()
    raise NotImplementedError("real SMS adapter (Eskiz) arrives in Task 5")
