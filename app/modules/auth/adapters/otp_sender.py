"""OTP delivery seam (SMS/email). Real SMS is Eskiz (3.5/5.4); email provider TBD.
The mock logs the code and keeps it in memory so dev and tests can read it."""

from functools import lru_cache
from typing import Protocol

import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)


class OtpSender(Protocol):
    async def send(self, *, target_type: str, target: str, code: str) -> None: ...


class MockOtpSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, *, target_type: str, target: str, code: str) -> None:
        self.sent.append((target_type, target, code))
        logger.info("otp.mock_send", target_type=target_type, target=target, code=code)


@lru_cache(maxsize=1)
def get_otp_sender() -> OtpSender:
    if get_settings().sms_mode == "mock":
        return MockOtpSender()
    raise NotImplementedError("real SMS adapter (Eskiz) arrives at stage 3.5/5.4")
