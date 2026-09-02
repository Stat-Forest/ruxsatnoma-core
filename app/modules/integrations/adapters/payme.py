"""Payme adapter seam (design/04 §3, plan `03.10a-payments-core` tasks 4-5).

Unlike OneID/E-IMZO/Eskiz, Payme's DIRECTION is reversed for the five
transactional methods task 4 implements (`payments/payme.py`,
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

Task 5 extends this SAME seam with two more concerns, rather than opening a
new one:

- **`ChangePassword` rotation** (`payments.payme._change_password` is the
  only writer): `verify()` now checks `settings_store`'s
  `CASHBOX_KEY_HASH_SETTING` override FIRST — a non-empty stored hash means
  the key has been rotated since deploy, and the PRESENTED key is compared
  against it (`sha256`, never the plaintext); an EMPTY override (the normal
  case — nobody has ever called `ChangePassword`) falls back to the
  env-configured `key` exactly as before. `verify()` is therefore async and
  takes `db` now, the one shape change to `PaymeAdapter` this task makes.
- **The checkout-redirect URL** (§3.8, `POST /invoices/{id}/pay-intents`):
  `build_checkout_url` reads `payme_merchant_id` itself (mirrors
  `get_payme_adapter()` reading `payme_cashbox_key` itself) and is pure
  string construction — no network call, so nothing about it branches on
  `payme_mode`; a mock deployment's determinism comes from ITS OWN
  configured merchant id, never a fake shape (ruling). `to_tiyin`/
  `from_tiyin` move here from `payments.payme` (task 4) so this function can
  reuse them without `integrations` (level 0) importing `payments`
  (level 4) — the wrong direction (design/01 §"Module levels"). `payments.
  payme` re-exports both names, so its own task-4 tests keep calling
  `payme.to_tiyin`/`payme.from_tiyin` unchanged.
"""

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import settings_store

_BASIC_PREFIX = "Basic "

# settings_store.py SETTING_SPECS key — shared between this file's own
# verify() (reader) and payments.payme._change_password (the only writer),
# so the two never drift on the string literal.
CASHBOX_KEY_HASH_SETTING = "payme_cashbox_key_hash"

_TIYIN = Decimal("0.01")


def to_tiyin(amount: Decimal) -> int:
    """so'm -> tiyin (design/04 §3.8: Payme amounts are integers, x100)."""
    return int((amount * 100).to_integral_exact(rounding=ROUND_HALF_UP))


def from_tiyin(tiyin: int) -> Decimal:
    """tiyin -> so'm, quantized to the same 2-decimal scale as
    `invoices.amount`/`provider_transactions.amount` (`Numeric(18, 2)`)."""
    return (Decimal(tiyin) / 100).quantize(_TIYIN)


class PaymeAuthError(Exception):
    """The Authorization header is missing, malformed, or does not carry the
    configured cashbox key. `payme_router.py` maps this — and ONLY this —
    to `-32504` in a 200 response (ruling E); it must never surface as
    anything else."""


class PaymeAdapter(Protocol):
    async def verify(self, db: AsyncSession, authorization_header: str | None) -> None:
        """Raise `PaymeAuthError` unless `authorization_header` is exactly
        `Basic base64("Paycom:<the EFFECTIVE cashbox key>")` — the rotated
        `settings_store` hash when one has ever been set, the env-configured
        key otherwise (module docstring)."""
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

    async def verify(self, db: AsyncSession, authorization_header: str | None) -> None:
        parsed = _parse_basic_auth(authorization_header)
        if parsed is None:
            raise PaymeAuthError("missing or malformed Authorization header")
        login, presented_key = parsed
        if login != "Paycom":
            raise PaymeAuthError("invalid credentials")
        # Ruling E: BYTES, never str — `hmac.compare_digest` raises
        # `TypeError` on a non-ASCII str operand, which would turn a
        # crafted header into the one 500 this whole route exists to
        # prevent (lesson: the identical trap on Eskiz's callback secret,
        # `notifications/webhooks_router.py`). Applies to BOTH branches
        # below, hash and plaintext alike.
        stored_hash = await settings_store.get_str(db, CASHBOX_KEY_HASH_SETTING)
        if stored_hash:
            presented_hash = hashlib.sha256(presented_key.encode("utf-8")).hexdigest()
            valid_key = hmac.compare_digest(
                presented_hash.encode("utf-8"), stored_hash.encode("utf-8")
            )
        else:
            valid_key = hmac.compare_digest(presented_key.encode("utf-8"), self.key.encode("utf-8"))
        if not valid_key:
            raise PaymeAuthError("invalid credentials")


def get_payme_adapter() -> PaymeAdapter:
    key = get_settings().payme_cashbox_key
    if not key:
        # Settings itself forbids reaching here under payme_mode=real
        # (ruling K) — only reachable with payme_mode=mock and no key
        # configured at all, i.e. a deployment that never set Payme up.
        # Refuse every call rather than comparing against an empty string,
        # which `compare_digest` would happily accept. Independent of
        # whether a rotation hash exists — a deployment that never set
        # PAYME_CASHBOX_KEY at all has no business serving this route
        # regardless of what ChangePassword may have done since.
        raise PaymeAuthError("Payme is not configured")
    return _CashboxKeyAdapter(key=key)


def build_checkout_url(*, invoice_number: str, amount: Decimal) -> str:
    """The hosted-checkout redirect (design/04 §3.8): `ac.id` is the account
    field this cashbox is configured with (the old system's own choice,
    carried forward) — the invoice NUMBER, matching what
    `payme._check_invoice_for_payment` resolves `account.id` against on the
    RPC side. Pure string construction, no network call, so nothing here is
    `payme_mode`-sensitive (ruling) — a mock deployment's determinism comes
    from its OWN configured `payme_merchant_id`, never a fake shape.
    """
    merchant_id = get_settings().payme_merchant_id
    if not merchant_id:
        # payme_mode=real and app_env=prod both already forbid this at the
        # Settings level (mirrors get_payme_adapter()'s own guard) —
        # reachable only in a payme_mode=mock deployment nobody finished
        # configuring. Refuse loudly rather than embedding the literal text
        # "None" in a link an applicant is about to be redirected to.
        raise RuntimeError("PAYME_MERCHANT_ID is not configured")
    payload = f"m={merchant_id};ac.id={invoice_number};a={to_tiyin(amount)}"
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return f"https://checkout.paycom.uz/{encoded}"
