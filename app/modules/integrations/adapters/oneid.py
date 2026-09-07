"""OneID adapter seam (moved from auth to integrations at stage 3.4, design/01).

The real sso.egov.uz exchange (response_type=one_code -> one_authorization_code ->
one_access_token_identify) is stage 5.1. Field names mirror the live system's
response (pin, first/sur/mid name, mob_phone_no, legal_info[].le_tin/le_name/
is_basic) and MUST be re-verified against current OneID docs before the real
adapter is written.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

from app.config import get_settings
from app.modules.integrations.adapters.mock_codec import decode_payload, encode_payload


@dataclass(frozen=True)
class OneIdLegalInfo:
    le_tin: str
    le_name: str
    is_basic: bool = False


@dataclass(frozen=True)
class OneIdProfile:
    """Profile snapshot from the identify step.

    NAMING TRAP (design/04 §1.3): in OneID, `sur_name` is the PATRONYMIC
    (Otasining ismi) and `mid_name` is the FAMILY NAME (Familiyasi) — the
    opposite of what the English words suggest. Nothing may render these
    without minding that mapping (C2/C3 autofill, stage 3.9)."""

    pinfl: str
    full_name: str
    first_name: str | None = None
    sur_name: str | None = None  # patronymic, NOT the surname
    mid_name: str | None = None  # family name
    birth_date: str | None = None
    phone: str | None = None
    passport: str | None = None
    auth_method: str | None = None  # LOGINPASSMETHOD|MOBILEMETHOD|PKCSMETHOD|LEPKCSMETHOD
    pkcs_legal_tin: str | None = None  # org STIR; present only with LEPKCSMETHOD
    valid: bool | None = None  # account once confirmed with an ERI
    user_id: str | None = None  # the person's OneID login
    legal_info: tuple[OneIdLegalInfo, ...] = field(default_factory=tuple)

    def to_snapshot(self) -> dict[str, Any]:
        data = asdict(self)
        data["legal_info"] = [asdict(li) for li in self.legal_info]
        return data

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> OneIdProfile:
        legal = tuple(OneIdLegalInfo(**li) for li in data.get("legal_info", []))
        return cls(**{**data, "legal_info": legal})


@dataclass(frozen=True)
class OneIdCall:
    """One provider round trip, for `integration_log` (stage 5.1 task 6).

    Carries no personal data and no token — only which grant it was, how it
    went, and the provider's own error code when it refused."""

    endpoint: str  # the grant_type: one_authorization_code|one_access_token_identify|one_log_out
    http_status: int | None
    duration_ms: int
    # OneID refuses with HTTP 400 and {"message": ..., "error": ...} — e.g.
    # "ClientSecretException" / "CLIENT_SECRET_NOT_FOUND" (verified against the
    # live endpoint 2026-09-07, plan 05.1 R7). These two strings are what tells
    # an administrator our secret is wrong rather than the provider being down.
    provider_message: str | None = None
    provider_error: str | None = None


@dataclass(frozen=True)
class OneIdLogin:
    """What one completed exchange yields.

    The access token sits HERE rather than on `OneIdProfile` deliberately: the
    profile is serialized whole into `users.oneid_profile`
    (`auth.service.login_or_create_by_pinfl`), a column read back for the
    director_registry basis and partly returned to the browser. A bearer token
    for a state system has no business inside it."""

    profile: OneIdProfile
    access_token: str | None = None
    calls: tuple[OneIdCall, ...] = ()


class OneIdError(Exception):
    """Provider unreachable or returned an error; err_code is ERR-INT-001/002.

    `calls` carries whatever round trips were made before the failure, so a
    refusal is as loggable as a success — the log entry is what tells the
    difference between a wrong secret and an outage."""

    def __init__(self, err_code: str, calls: tuple[OneIdCall, ...] = ()) -> None:
        super().__init__(err_code)
        self.err_code = err_code
        self.calls = calls


class OneIdAdapter(Protocol):
    def authorize_url(self, *, state: str, redirect_uri: str, scope: str) -> str: ...

    async def exchange_code(self, code: str) -> OneIdLogin: ...

    async def logout(self, access_token: str | None) -> None: ...


def encode_mock_code(profile: OneIdProfile) -> str:
    """Dev/test helper: a mock `code` IS the base64url profile (ruling 3)."""
    return encode_payload(profile.to_snapshot())


def provider_authorize_url(*, state: str, redirect_uri: str, scope: str) -> str:
    """The real sso.egov.uz `Authorization.do` request shape (field names to be
    re-verified against current OneID docs before the stage 5.1 real adapter is
    written — see the module docstring). This used to be what
    `MockOneId.authorize_url` itself returned, which sent a browser clicking the
    adminka's OneID button to the real provider even with `ONEID_MODE=mock` —
    a dead end with no route back (final review of stage 6.6, finding 1).
    Nothing calls this today (`get_oneid_adapter()` still raises
    `NotImplementedError` for `oneid_mode=real`); it is kept, and pinned by a
    test, purely so that shape is not lost and stage 5.1 has it to reproduce."""
    query = urlencode(
        {
            "response_type": "one_code",
            "client_id": "mock",
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scope,
        }
    )
    return f"https://sso.egov.uz/sso/oauth/Authorization.do?{query}"


# The identity a click on the mock OneID button logs a browser in as. Never a
# real citizen's PINFL — no PINFL-generation scheme in use produces fourteen
# identical digits, the same reasoning behind the reserved prefixes
# `app/seed/demo.py`'s own DEMO_STAFF/DEMO_APPLICANT rows document. A
# constant, not a setting: `oneid_mode=mock` never runs in prod
# (`_forbid_default_secret_in_prod` forbids it), so no deployment would ever
# want a different demo persona here — this identity exists only to prove the
# redirect chain is walkable in a browser, not to pick who is being demoed.
MOCK_DEMO_PROFILE = OneIdProfile(
    pinfl="99999999999999",
    full_name="MOCK ONEID DEMO",
    phone="+998900000000",
)


class MockOneId:
    def authorize_url(self, *, state: str, redirect_uri: str, scope: str) -> str:
        """Points a browser back at THIS application's own callback with a
        walkable demo `code`, never at the real provider — before this, the
        adminka's OneID tab sent a citizen's whole browser tab to
        sso.egov.uz's real error page with no way back, on every environment
        that exists today (final review of stage 6.6, finding 1). `scope` is
        accepted only to match `OneIdAdapter`'s shape: the callback this URL
        targets reads just `code` and `state`, and `redirect_uri` is reused
        verbatim rather than reconstructed, so this always lands exactly where
        the real provider was configured to."""
        code = encode_mock_code(MOCK_DEMO_PROFILE)
        query = urlencode({"code": code, "state": state})
        return f"{redirect_uri}?{query}"

    async def exchange_code(self, code: str) -> OneIdLogin:
        try:
            return OneIdLogin(profile=OneIdProfile.from_snapshot(decode_payload(code)))
        except (ValueError, TypeError) as exc:
            raise OneIdError("ERR-INT-002") from exc

    async def logout(self, access_token: str | None) -> None:
        """No-op mock. The real one_log_out call arrives at stage 5.1 — we do not
        store the access token yet, so auth.service does not call this method
        until then."""
        return None


def get_oneid_adapter() -> OneIdAdapter:
    if get_settings().oneid_mode == "mock":
        return MockOneId()
    raise NotImplementedError("real OneID adapter arrives at stage 5.1")
