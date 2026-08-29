"""SMS delivery seam. The real provider is Eskiz (design/04 §4); it is added in
Task 5 of plan 03.5 and switched on by `sms_mode=real` at stage 5.4."""

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Protocol

import httpx
import structlog

from app.config import Settings, get_settings

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


class EskizError(Exception):
    """Delivery failure. The message carries a status and a reason class ONLY —
    never the phone number and never the SMS text (which for an OTP is the code):
    it ends up in `outbox_messages.last_error`, which administrators can read."""


class EskizSmsSender:
    """Eskiz.uz client (design/04 §4).

    A fresh httpx client per call: SMS volume is low, and a long-lived pooled
    client bound to one event loop is a known source of cross-loop breakage in a
    process that also runs workers. The JWT (≈30 days) IS cached in the instance
    and re-fetched on a 401 or once it is older than TOKEN_MAX_AGE.
    """

    TOKEN_MAX_AGE = timedelta(days=29)
    TIMEOUT_SECONDS = 10.0  # tz/09: synchronous timeout ≤ 10 s

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport  # tests inject httpx.MockTransport
        self._token: str | None = None
        self._token_at: datetime | None = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._settings.eskiz_base_url,
            timeout=self.TIMEOUT_SECONDS,
            transport=self._transport,
        )

    @property
    def callback_url(self) -> str:
        return (
            f"{self._settings.public_base_url.rstrip('/')}"
            f"/api/v1/webhooks/eskiz/{self._settings.eskiz_callback_secret}"
        )

    async def _login(self, client: httpx.AsyncClient) -> str:
        try:
            response = await client.post(
                "/api/auth/login",
                data={
                    "email": self._settings.eskiz_email,
                    "password": self._settings.eskiz_password,
                },
            )
        except httpx.HTTPError as exc:
            raise EskizError(f"eskiz login transport error: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise EskizError(f"eskiz login failed: HTTP {response.status_code}")
        token = (response.json().get("data") or {}).get("token")
        if not token:
            raise EskizError("eskiz login returned no token")
        self._token, self._token_at = token, datetime.now(UTC)
        return token

    async def _ensure_token(self, client: httpx.AsyncClient) -> str:
        fresh = (
            self._token is not None
            and self._token_at is not None
            and datetime.now(UTC) - self._token_at < self.TOKEN_MAX_AGE
        )
        return self._token if fresh and self._token else await self._login(client)

    async def send(self, *, phone: str, text: str, reference: str) -> str | None:
        digits = "".join(ch for ch in phone if ch.isdigit())
        payload = {
            "mobile_phone": digits,
            "message": text,
            "from": self._settings.eskiz_sender,
            "callback_url": self.callback_url,
            "user_sms_id": reference,
        }
        async with self._client() as client:
            token = await self._ensure_token(client)
            response = await self._post_send(client, payload, token)
            if response.status_code == 401:
                # The token can expire early (a password change on their side).
                response = await self._post_send(client, payload, await self._login(client))
            if response.status_code >= 400:
                raise EskizError(f"eskiz send failed: HTTP {response.status_code}")
            logger.info("sms.eskiz_send", reference=reference, phone=_mask(digits))
            body = response.json() if response.content else {}
        message_id = body.get("id") or (body.get("data") or {}).get("id")
        return str(message_id) if message_id is not None else None

    async def _post_send(
        self, client: httpx.AsyncClient, payload: dict[str, str], token: str
    ) -> httpx.Response:
        try:
            return await client.post(
                "/api/message/sms/send",
                data=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise EskizError(f"eskiz send transport error: {type(exc).__name__}") from None


def _mask(digits: str) -> str:
    """Phone numbers are personal data; log the tail only (auth._mask_target idiom)."""
    return f"***{digits[-4:]}" if len(digits) > 4 else "***"


@lru_cache(maxsize=1)
def get_sms_sender() -> SmsSender:
    settings = get_settings()
    if settings.sms_mode == "mock":
        return MockSmsSender()
    return EskizSmsSender(settings)
