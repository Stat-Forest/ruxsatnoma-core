"""SMS delivery seam. The real provider is Eskiz (design/04 §4); it is added in
Task 5 of plan 03.5 and switched on by `sms_mode=real` at stage 5.4."""

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Protocol

import httpx
import structlog

from app.config import Settings, get_settings

logger = structlog.get_logger(__name__)


# Eskiz validates `user_sms_id` as a number of at most twelve digits — measured
# live on 2026-09-14: `999999999999` accepted, `1000000000000`, a uuid and a
# uuid's hex refused with `400 user_sms_id is invalid`.
MAX_REFERENCE_DIGITS = 12


class SmsSender(Protocol):
    async def send(
        self, *, phone: str, text: str, reference: str | None = None, delivery_report: bool = True
    ) -> str | None:
        """Deliver one SMS. `reference` is our own correlation id — the
        notification's numeric `provider_reference`, as decimal digits — echoed
        back by the provider's delivery report. Returns the provider's message id
        when it supplies one. Raises on failure — the outbox retries.

        `reference=None` with `delivery_report=False` is a send that has no
        `notifications` row to correlate a report against — OTP today. A report
        we cannot correlate is a dead letter, not information, so such a send
        carries neither an id for the provider to echo nor a callback."""
        ...


class MockSmsSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    async def send(
        self, *, phone: str, text: str, reference: str | None = None, delivery_report: bool = True
    ) -> str | None:
        self.sent.append((phone, text, reference))
        # Masked exactly like the Eskiz sender: this class is the copy-paste source
        # for the next adapter, and a phone number is personal data in either.
        logger.info("sms.mock_send", phone=_mask(phone), reference=reference)
        return f"mock-{reference}"


class EskizError(Exception):
    """Delivery failure. The message carries the HTTP status, a reason class, and
    — when the provider gave one — ITS OWN explanation of the refusal. It carries
    never the phone number and never the SMS text (which for an OTP is the code):
    it ends up in `outbox_messages.last_error`, which administrators read through
    `/admin/integrations/*`. That the provider's explanation is safe to carry is
    guaranteed mechanically, by `_failure_reason` below — never by trusting the
    provider to keep our own request out of its error body."""


# Fields of a JSON error body that may become the reason. An allow-list, so a
# provider that echoes the whole request back cannot widen it. `status` is left
# out deliberately: Eskiz's value there is the literal word "error", which the
# HTTP status already says.
_REASON_FIELDS = ("message", "error", "description", "detail", "error_code", "code")
# `admin/export.py::_ERROR_MAX` truncates `last_error` to 200 characters in the
# XLSX an administrator reads, so a longer reason would not be visible there
# anyway; the real Eskiz moderation message is 149.
_REASON_MAX_CHARS = 200
# Below this length a literal replacement shreds unrelated words rather than
# hiding anything ("hi" appears inside half the Latin alphabet's words); short
# values are left to the digit scrub, which is what an OTP code is made of.
_MIN_REDACTED_CHARS = 4
_REDACTED = "[redacted]"
_DIGIT_RUN = re.compile(r"\d{4,}")


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """The response body as a JSON object, or `{}` for anything else — HTML, an
    empty body, a bare string, an array. Every read of a provider body goes
    through this: a 200 carrying an error page used to raise `JSONDecodeError`
    from inside `_login`, which reaches `last_error` as a decode error and says
    nothing about the provider."""
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _redact(text: str, *, sent: Sequence[str | None]) -> str:
    """Remove from `text` everything we handed the provider, then every run of
    four or more digits, then cap it."""
    for value in sent:
        if value and len(value) >= _MIN_REDACTED_CHARS:
            # Case-insensitively: a provider that upper-cases our text in its own
            # error message must not slip past an exact comparison.
            text = re.sub(re.escape(value), _REDACTED, text, flags=re.IGNORECASE)
    return _DIGIT_RUN.sub(_REDACTED, text)[:_REASON_MAX_CHARS]


def _failure_reason(response: httpx.Response, *, sent: Sequence[str | None]) -> str | None:
    """The provider's own explanation of a refusal, made safe to store.

    Eskiz answers a refusal with `{"message", "status", "id"}`, and the `message`
    is the only part that says WHY — «Этот смс текст еще не прошёл модерацию…».
    Without it `outbox_messages.last_error` read `eskiz send failed: HTTP 400`
    and nothing else, and learning the real reason took a throwaway script
    against the live provider (2026-09-23). Nothing in the contract promises that
    shape, so anything which is not a JSON object yields no reason at all and the
    caller still reports the status.

    `sent` is everything we just handed the provider. Three filters guard it,
    deliberately overlapping, because assuming a provider does NOT echo the
    request back is exactly the assumption that makes a leak invisible:

    1. the `_REASON_FIELDS` allow-list. Structural, so it holds whatever the
       provider writes: a whole-request echo lands in a nested object, or in a
       field nobody named, and never reaches the string at all.
    2. `_redact` over what survives — the exact values of `sent`, then every run
       of four or more digits. The second half is the filter that needs no
       knowledge of what to look for: an OTP code reprinted on its own, or a
       phone number the provider reformatted, is caught by SHAPE. The HTTP status
       is added by the caller, outside this string, so nothing useful is lost.
    3. the length cap in `_redact`, so an HTML error page or a JSON dump cannot
       become the "reason" and push the useful part out of `last_error`'s 1000.

    Redacting rather than dropping the field whole is on purpose: the case worth
    reading is precisely a provider quoting our text INSIDE its explanation, and
    a `[redacted]` marker there still leaves the sentence around it legible.

    Residual risk, stated rather than hidden: a provider that PARAPHRASES our
    text — re-wrapped, partially quoted, transliterated — defeats filter 2, and
    filter 1 is what stands. That is why the allow-list is the guarantee and the
    redaction the belt-and-braces, and not the other way round.
    """
    body = _json_object(response)
    parts: list[str] = []
    for field in _REASON_FIELDS:
        value = body.get(field)
        if isinstance(value, str | int) and not isinstance(value, bool) and str(value).strip():
            parts.append(str(value).strip())
    return _redact("; ".join(parts), sent=sent) or None


def _failure_message(what: str, status: int, reason: str | None) -> str:
    """One shape for every provider refusal: what we were doing, the status, and
    the provider's reason when there is one that survived `_failure_reason`."""
    head = f"{what}: HTTP {status}"
    return f"{head}: {reason}" if reason else head


def _transport_failure(what: str, exc: httpx.HTTPError, *, sent: Sequence[str | None]) -> str:
    """A failure with no response at all. `type(exc).__name__` is mandatory —
    an asyncio-flavour timeout has an EMPTY `str()` (lessons: log `repr`, not
    `f"{e}"`) — and httpx's own text ("[Errno 61] Connection refused") is the
    difference between a diagnosable incident and a redeploy. It never carries
    the request body; it goes through the same scrub anyway rather than resting
    on that."""
    detail = _redact(str(exc), sent=sent).strip()
    head = f"{what}: {type(exc).__name__}"
    return f"{head}: {detail}" if detail else head


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
        # Ruling #124: this is a machine endpoint Eskiz's own server calls, never
        # the QR link a citizen's browser opens — `eskiz_callback_base_url`, never
        # `public_base_url` (the two split at stage 7.0's own dev/prod separation).
        return (
            f"{self._settings.eskiz_callback_base_url.rstrip('/')}"
            f"/api/v1/webhooks/eskiz/{self._settings.eskiz_callback_secret}"
        )

    async def _login(self, client: httpx.AsyncClient) -> str:
        credentials = (self._settings.eskiz_email, self._settings.eskiz_password)
        try:
            response = await client.post(
                "/api/auth/login",
                data={
                    "email": self._settings.eskiz_email,
                    "password": self._settings.eskiz_password,
                },
            )
        except httpx.HTTPError as exc:
            raise EskizError(
                _transport_failure("eskiz login transport error", exc, sent=credentials)
            ) from None
        if response.status_code != 200:
            reason = _failure_reason(response, sent=credentials)
            raise EskizError(_failure_message("eskiz login failed", response.status_code, reason))
        data = _json_object(response).get("data")
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            # A 200 that carries no usable token is still the provider refusing
            # us, and its body is the only thing that can say why (a blocked
            # account answers 200 here).
            reason = _failure_reason(response, sent=credentials)
            raise EskizError(
                _failure_message("eskiz login returned no token", response.status_code, reason)
            )
        self._token, self._token_at = token, datetime.now(UTC)
        return token

    async def _ensure_token(self, client: httpx.AsyncClient) -> str:
        fresh = (
            self._token is not None
            and self._token_at is not None
            and datetime.now(UTC) - self._token_at < self.TOKEN_MAX_AGE
        )
        return self._token if fresh and self._token else await self._login(client)

    async def send(
        self, *, phone: str, text: str, reference: str | None = None, delivery_report: bool = True
    ) -> str | None:
        if reference is not None and not (
            reference.isdigit() and len(reference) <= MAX_REFERENCE_DIGITS
        ):
            # Our own code shaped the id wrongly — a defect, not a provider
            # failure: never an `EskizError` the outbox would retry to death.
            raise ValueError(f"sms reference must be 1-{MAX_REFERENCE_DIGITS} digits")
        digits = "".join(ch for ch in phone if ch.isdigit())
        payload = {
            "mobile_phone": digits,
            "message": text,
            "from": self._settings.eskiz_sender,
        }
        if reference is not None:
            payload["user_sms_id"] = reference
        # Eskiz posts a report for every message that carries a callback_url. An OTP
        # send has no `notifications` row behind its reference, so its report would
        # dead-letter every single time — one inbound_dead_letters row (holding the
        # recipient's phone number) plus one integration_log row per phone
        # verification, forever, with no purge job covering dead letters. Do not ask
        # for a report we would only have to throw away.
        if delivery_report:
            payload["callback_url"] = self.callback_url
        async with self._client() as client:
            token = await self._ensure_token(client)
            response = await self._post_send(client, payload, token)
            if response.status_code == 401:
                # The token can expire early (a password change on their side).
                response = await self._post_send(client, payload, await self._login(client))
            if response.status_code >= 400:
                # Everything we just handed the provider that may not come
                # back: the recipient in the caller's form as well as the wire's
                # (a provider that reformats the number defeats a digits-only
                # comparison), the text, and the two credentials — the JWT, and
                # the callback secret that `callback_url` carries in the payload
                # and that is the ONLY authentication on our webhook. `from` is
                # deliberately absent: a sender id is neither personal data nor a
                # secret, and "sender X is not approved" is worth reading whole.
                sent = (
                    digits,
                    phone,
                    text,
                    self._token,
                    self._settings.eskiz_callback_secret,
                )
                reason = _failure_reason(response, sent=sent)
                logger.warning(
                    "sms.eskiz_send_failed",
                    reference=reference,
                    phone=_mask(digits),
                    status=response.status_code,
                    reason=reason,
                )
                raise EskizError(
                    _failure_message("eskiz send failed", response.status_code, reason)
                )
            logger.info("sms.eskiz_send", reference=reference, phone=_mask(digits))
            body = _json_object(response)
        # `data` is a dict on Eskiz's own success body, but a provider free to
        # answer anything may put a string or a list there — `_json_object`
        # guards the top level, this guards the one level below it.
        data = body.get("data")
        message_id = body.get("id") or (data.get("id") if isinstance(data, dict) else None)
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
            raise EskizError(
                _transport_failure(
                    "eskiz send transport error", exc, sent=(*payload.values(), token)
                )
            ) from None


def _mask(phone: str) -> str:
    """Phone numbers are personal data; log the tail only (auth._mask_target idiom).
    Strips separators itself, so a caller may pass a raw or an already-digits form."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"***{digits[-4:]}" if len(digits) > 4 else "***"


@lru_cache(maxsize=1)
def get_sms_sender() -> SmsSender:
    settings = get_settings()
    if settings.sms_mode == "mock":
        return MockSmsSender()
    return EskizSmsSender(settings)
