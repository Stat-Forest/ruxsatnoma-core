"""OTP delivery seam (SMS/email). Real SMS is Eskiz, real email is SMTP (3.5 Task
6). The mock logs the code and keeps it in memory so dev and tests can read it."""

import uuid
from functools import lru_cache
from typing import Protocol

import structlog

from app.config import get_settings
from app.modules.integrations.adapters.email import get_email_sender
from app.modules.integrations.adapters.sms import get_sms_sender

logger = structlog.get_logger(__name__)


class OtpSender(Protocol):
    async def send(self, *, target_type: str, target: str, code: str) -> None: ...


class MockOtpSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, *, target_type: str, target: str, code: str) -> None:
        self.sent.append((target_type, target, code))
        logger.info("otp.mock_send", target_type=target_type, target=target, code=code)


# Latin Uzbek deliberately: 160 characters per SMS part instead of 70 for Cyrillic,
# and an OTP request may be anonymous, so there is no recipient language to honour.
# This exact text is what we register for Eskiz moderation (design/04 §4).
OTP_TEXT = "Ruxsatnoma: tasdiqlash kodi {code}. Kodni hech kimga bermang."
OTP_SUBJECT = "Ruxsatnoma: tasdiqlash kodi"


class RealOtpSender:
    """Routes an OTP to the channel its target implies. `auth` (level 1) keeps one
    seam and never learns which providers exist (plan 03.5 ruling 1)."""

    async def send(self, *, target_type: str, target: str, code: str) -> None:
        text = OTP_TEXT.format(code=code)
        if target_type == "phone":
            await get_sms_sender().send(phone=target, text=text, reference=str(uuid.uuid4()))
        else:
            await get_email_sender().send(to=target, subject=OTP_SUBJECT, text=text)


@lru_cache(maxsize=1)
def get_otp_sender() -> OtpSender:
    if get_settings().sms_mode == "mock":
        return MockOtpSender()
    return RealOtpSender()
