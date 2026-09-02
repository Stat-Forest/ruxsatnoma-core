"""Payme adapter seam (design/04 §3, plan `03.10a-payments-core` task 4).

Unlike OneID/E-IMZO/Eskiz, Payme's DIRECTION is reversed for the five
transactional methods this task implements (`payments/payme.py`,
`payments/payme_router.py`): Payme is the CLIENT and calls US, so there is
no outbound round-trip for an adapter to wrap here — nothing in this file
makes an HTTP request.

What DOES belong here, mirroring the `*_MODE` idiom of `oneid.py`, is
verifying the Basic-Auth credentials Payme presents on every call
(design/04 §3.1, ruling E). It is genuinely mode-sensitive, just not in its
ALGORITHM — comparing a shared secret needs no network round-trip in either
mode — but in WHICH key is compared against: `payme_mode=mock` points at
Payme's own SANDBOX cashbox key, `=real` at the production one (design/04
§3.10 — Payme's sandbox is real infrastructure with its own test key,
unlike OneID's `localhost`-only mock, which has nothing "real" to point at
during local development). `Settings` itself enforces the rest of the mode
contract (`payme_mode=real` requires both `payme_merchant_id` and
`payme_cashbox_key`; `app_env=prod` forbids `payme_mode=mock`), so this
module needs no branch of its own beyond reading
`get_settings().payme_cashbox_key` — one adapter class serves both modes.

`payme_merchant_id` is read by nothing in this file: it is Task 5's
checkout-redirect concern (§3.8, `POST /invoices/{id}/pay-intents`), kept in
`Settings` because Task 5 extends this SAME adapter seam rather than opening
a new one.
"""

import base64
import binascii
import hmac
from dataclasses import dataclass
from typing import Protocol

from app.config import get_settings

_BASIC_PREFIX = "Basic "


class PaymeAuthError(Exception):
    """The Authorization header is missing, malformed, or does not carry the
    configured cashbox key. `payme_router.py` maps this — and ONLY this —
    to `-32504` in a 200 response (ruling E); it must never surface as
    anything else."""


class PaymeAdapter(Protocol):
    def verify(self, authorization_header: str | None) -> None:
        """Raise `PaymeAuthError` unless `authorization_header` is exactly
        `Basic base64("Paycom:<cashbox key>")` for the configured mode."""
        ...


def _parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header or not header.startswith(_BASIC_PREFIX):
        return None
    try:
        decoded = base64.b64decode(header[len(_BASIC_PREFIX) :], validate=True).decode("utf-8")
    except binascii.Error, ValueError, UnicodeDecodeError:
        return None
    login, sep, key = decoded.partition(":")
    if not sep:
        return None
    return login, key


@dataclass(frozen=True)
class _CashboxKeyAdapter:
    key: str

    def verify(self, authorization_header: str | None) -> None:
        parsed = _parse_basic_auth(authorization_header)
        if parsed is None:
            raise PaymeAuthError("missing or malformed Authorization header")
        login, key = parsed
        # Ruling E: BYTES, never str — `hmac.compare_digest` raises
        # `TypeError` on a non-ASCII str operand, which would turn a
        # crafted header into the one 500 this whole route exists to
        # prevent (lesson: the identical trap on Eskiz's callback secret,
        # `notifications/webhooks_router.py`).
        valid_key = hmac.compare_digest(key.encode("utf-8"), self.key.encode("utf-8"))
        if login != "Paycom" or not valid_key:
            raise PaymeAuthError("invalid credentials")


def get_payme_adapter() -> PaymeAdapter:
    key = get_settings().payme_cashbox_key
    if not key:
        # Settings itself forbids reaching here under payme_mode=real
        # (ruling K) — only reachable with payme_mode=mock and no key
        # configured at all, i.e. a deployment that never set Payme up.
        # Refuse every call rather than comparing against an empty string,
        # which `compare_digest` would happily accept.
        raise PaymeAuthError("Payme is not configured")
    return _CashboxKeyAdapter(key=key)
