"""OTP delivery seam (SMS/email). Real SMS is Eskiz, real email is SMTP (3.5 Task
6). The mock logs the code and keeps it in memory so dev and tests can read it."""

from functools import lru_cache
from typing import Protocol

import structlog

from app.config import get_settings
from app.modules.integrations.adapters.email import get_email_sender
from app.modules.integrations.adapters.sms import get_sms_sender

logger = structlog.get_logger(__name__)


class OtpSender(Protocol):
    async def send(
        self, *, target_type: str, target: str, code: str, purpose: str | None = None
    ) -> None: ...


class MockOtpSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(
        self, *, target_type: str, target: str, code: str, purpose: str | None = None
    ) -> None:
        self.sent.append((target_type, target, code))
        logger.info(
            "otp.mock_send", target_type=target_type, target=target, code=code, purpose=purpose
        )


# Latin Uzbek deliberately: 160 characters per SMS part instead of 70 for Cyrillic,
# and an OTP request may be anonymous, so there is no recipient language to honour.
# One text PER PURPOSE, each verbatim what is registered for Eskiz moderation: a
# confirmation code must name the resource and what the code is for, or the
# operators refuse it — the single shared «Ruxsatnoma: tasdiqlash kodi …» was
# rejected on 2026-09-24. The domain is the prod one on every stand: a moderated
# text is fixed, and dev sends through the same Eskiz account.
OTP_TEXTS: dict[str, str] = {
    "phone_verify": (
        "ruxsatnoma-urmon.uz saytida telefon raqamingizni tasdiqlash uchun kod: {code}."
        " Kodni hech kimga bermang."
    ),
    "email_verify": (
        "ruxsatnoma-urmon.uz saytida elektron pochtangizni tasdiqlash uchun kod: {code}."
        " Kodni hech kimga bermang."
    ),
    "password_reset": (
        "ruxsatnoma-urmon.uz saytida parolni tiklash uchun kod: {code}. Kodni hech kimga bermang."
    ),
}
# A row queued before the purpose reached the outbox payload carries none.
_DEFAULT_PURPOSE = {"phone": "phone_verify", "email": "email_verify"}
OTP_SUBJECT = "Ruxsatnoma: tasdiqlash kodi"


def otp_text(*, target_type: str, code: str, purpose: str | None) -> str:
    template = OTP_TEXTS.get(purpose or "") or OTP_TEXTS[_DEFAULT_PURPOSE[target_type]]
    return template.format(code=code)


class RealOtpSender:
    """Routes an OTP to the channel its target implies. `auth` (level 1) keeps one
    seam and never learns which providers exist (plan 03.5 ruling 1)."""

    async def send(
        self, *, target_type: str, target: str, code: str, purpose: str | None = None
    ) -> None:
        text = otp_text(target_type=target_type, code=code, purpose=purpose)
        if target_type == "phone":
            # No reference and no delivery report: an OTP has no `notifications`
            # row, so every report Eskiz posted for it would be dead-lettered (with
            # the phone number in the stored payload), and any id we invented for
            # it would name nothing. OTP success is the user entering the code,
            # never a provider callback.
            await get_sms_sender().send(
                phone=target,
                text=text,
                reference=None,
                delivery_report=False,
            )
        else:
            await get_email_sender().send(to=target, subject=OTP_SUBJECT, text=text)


@lru_cache(maxsize=1)
def get_otp_sender() -> OtpSender:
    """Mock only when BOTH sms_mode and email_mode are mock. Either one set to
    real must route through RealOtpSender — otherwise an explicitly-configured
    real channel would be silently swallowed by the other, still-mocked switch
    (review finding, plan 03.5 Task 6). In practice only two combinations occur:
    both mock (dev/tests) and both real (prod, per Settings' prod guard)."""
    settings = get_settings()
    if settings.sms_mode == "mock" and settings.email_mode == "mock":
        return MockOtpSender()
    return RealOtpSender()
