"""OneID adapter seam (moved from auth to integrations at stage 3.4, design/01).

`RealOneId` (stage 5.1) speaks the live sso.egov.uz exchange: response_type=
one_code -> one_authorization_code -> one_access_token_identify, plus
one_log_out at our own logout. The contract was re-verified against the
provider itself on 2026-09-07 rather than against the 2022/2024 technical
instructions: step 1 answers 307 to id.egov.uz, refusals are HTTP 400 with
{"message", "error"}, `code` and `access_token` are UUIDs, and there is no
OIDC path (`/sso/.well-known/openid-configuration` -> 404).

`MockOneId` stays the default and cannot be retired: the TI forbids localhost
as a redirect target, so local development can never talk to the provider.
"""

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
import structlog

from app.config import Settings, get_settings
from app.modules.integrations.adapters.mock_codec import decode_payload, encode_payload

logger = structlog.get_logger(__name__)


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


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _normalize_phone(raw: Any) -> str | None:
    """OneID sends `998901234567`; ours is `+998XXXXXXXXX` everywhere
    (`auth/schemas.py`'s own pattern). A number left in the provider's shape
    would be stored and displayed, then REJECTED the first time the citizen
    edited their own profile — so normalize here, and drop what cannot be
    normalized rather than storing something no route of ours will accept."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) == 12 and digits.startswith("998"):
        return f"+{digits}"
    if len(digits) == 9:
        return f"+998{digits}"
    return None


def _as_bool(raw: Any) -> bool | None:
    """`valid` arrives as the string "true"/"false" as often as a JSON bool."""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return None
    return str(raw).strip().lower() in {"true", "1", "yes"}


def _legal_entry(entry: dict[str, Any]) -> OneIdLegalInfo | None:
    """`design/04` §1.3 records both spellings the TI uses (`le_tin`/`le_name`
    and `tin`/`acron_UZ`); the old system reads the `le_*` pair. Accept either.
    An entry with no STIR is dropped: it cannot be a representation basis, and
    it would render as an empty row in "apply on behalf of an organization"."""
    tin = str(entry.get("le_tin") or entry.get("tin") or "").strip()
    if not tin:
        return None
    name = str(entry.get("le_name") or entry.get("acron_UZ") or "").strip()
    return OneIdLegalInfo(le_tin=tin, le_name=name, is_basic=bool(_as_bool(entry.get("is_basic"))))


def parse_identify(payload: dict[str, Any]) -> OneIdProfile:
    """Map the identify response onto `OneIdProfile`.

    Every extended field is optional (plan 05.1 R6): what the provider returns
    today is evidence, not a promise, and the failure mode of assuming
    otherwise is silent — an empty autofill looks exactly like a citizen who
    typed nothing."""
    pinfl = str(payload.get("pin") or "").strip()
    if not pinfl:
        raise OneIdError("ERR-INT-002")
    first = str(payload.get("first_name") or "").strip()
    family = str(payload.get("mid_name") or "").strip()  # NOT a middle name
    patronymic = str(payload.get("sur_name") or "").strip()  # NOT a surname
    full_name = str(payload.get("full_name") or "").strip() or " ".join(
        part for part in (family, first, patronymic) if part
    )
    raw_legal = payload.get("legal_info") or []
    legal = tuple(
        entry
        for entry in (_legal_entry(item) for item in raw_legal if isinstance(item, dict))
        if entry is not None
    )
    return OneIdProfile(
        pinfl=pinfl,
        full_name=full_name,
        first_name=first or None,
        sur_name=patronymic or None,
        mid_name=family or None,
        birth_date=str(payload.get("birth_date") or "") or None,
        phone=_normalize_phone(payload.get("mob_phone_no")),
        passport=str(payload.get("pport_no") or payload.get("doc_num") or "") or None,
        auth_method=str(payload.get("auth_method") or "") or None,
        pkcs_legal_tin=str(payload.get("pkcs_legal_tin") or "") or None,
        valid=_as_bool(payload.get("valid")),
        user_id=str(payload.get("user_id") or "") or None,
        legal_info=legal,
    )


class RealOneId:
    """Live `sso.egov.uz` client (design/04 §1, re-verified against the
    provider on 2026-09-07).

    One endpoint serves all four steps; they differ only by `grant_type`. A
    fresh httpx client per exchange, for the same reason `EskizSmsSender` uses
    one: a pooled client bound to one event loop breaks in a process that also
    runs workers.

    There are NO retries. A login is synchronous — a citizen is watching a
    browser — and the TI caps us at 300 requests a minute; a retry loop during
    a provider outage spends that budget and still shows the same error.
    """

    TIMEOUT_SECONDS = 10.0  # tz/09's reliability rule, as for Eskiz

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport  # tests inject httpx.MockTransport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.TIMEOUT_SECONDS, transport=self._transport)

    def _credentials(self) -> dict[str, str]:
        return {
            "client_id": self._settings.oneid_client_id,
            "client_secret": self._settings.oneid_client_secret,
        }

    def authorize_url(self, *, state: str, redirect_uri: str, scope: str) -> str:
        query = urlencode(
            {
                "response_type": "one_code",
                "client_id": self._settings.oneid_client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": scope,
            }
        )
        return f"{self._settings.oneid_base_url}?{query}"

    async def _post(
        self, client: httpx.AsyncClient, data: dict[str, str], calls: list[OneIdCall]
    ) -> dict[str, Any]:
        started = time.monotonic()
        grant = data["grant_type"]
        try:
            response = await client.post(
                self._settings.oneid_base_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            calls.append(OneIdCall(grant, None, _ms(started)))
            logger.warning("oneid.transport_error", grant=grant, error=type(exc).__name__)
            raise OneIdError("ERR-INT-001", tuple(calls)) from None

        # A refusal is JSON even at 400 (R7), so read the body BEFORE deciding:
        # the provider's own code is what tells an administrator that our
        # secret is wrong rather than that OneID is down.
        try:
            payload = response.json()
        except ValueError:
            payload = None
        body = payload if isinstance(payload, dict) else None
        calls.append(
            OneIdCall(
                grant,
                response.status_code,
                _ms(started),
                provider_message=str(body["message"]) if body and body.get("message") else None,
                provider_error=str(body["error"]) if body and body.get("error") else None,
            )
        )

        if response.status_code >= 500:
            logger.warning("oneid.provider_unavailable", grant=grant, status=response.status_code)
            raise OneIdError("ERR-INT-001", tuple(calls))
        if response.status_code != 200:
            # A 4xx is a REFUSAL — a wrong secret, an expired code, an
            # unregistered redirect_uri. Waiting fixes none of them.
            logger.warning(
                "oneid.refused",
                grant=grant,
                status=response.status_code,
                provider_error=calls[-1].provider_error,
            )
            raise OneIdError("ERR-INT-002", tuple(calls))
        if body is None:
            raise OneIdError("ERR-INT-002", tuple(calls))
        return body

    async def exchange_code(self, code: str) -> OneIdLogin:
        calls: list[OneIdCall] = []
        async with self._client() as client:
            token_payload = await self._post(
                client,
                {
                    "grant_type": "one_authorization_code",
                    **self._credentials(),
                    "redirect_uri": self._settings.oneid_redirect_uri,
                    "code": code,
                },
                calls,
            )
            access_token = str(token_payload.get("access_token") or "")
            if not access_token:
                # A 200 carrying {"error": ...} — the provider refusing inside
                # a success status.
                raise OneIdError("ERR-INT-002", tuple(calls))
            identify = await self._post(
                client,
                {
                    "grant_type": "one_access_token_identify",
                    **self._credentials(),
                    "access_token": access_token,
                    "scope": self._settings.oneid_scope,
                },
                calls,
            )
        try:
            profile = parse_identify(identify)
        except OneIdError as exc:
            raise OneIdError(exc.err_code, tuple(calls)) from None
        return OneIdLogin(profile=profile, access_token=access_token, calls=tuple(calls))

    async def logout(self, access_token: str | None) -> None:
        """Ends the OneID session as well as ours (design/04 §1.2 step 4).

        Best effort by design: our own session is already revoked by the time
        this runs (`auth.service.logout_session`), and a provider failure must
        never turn "sign out" into an error for the citizen."""
        if not access_token:
            return None
        calls: list[OneIdCall] = []
        try:
            async with self._client() as client:
                await self._post(
                    client,
                    {
                        "grant_type": "one_log_out",
                        **self._credentials(),
                        "access_token": access_token,
                        "scope": self._settings.oneid_scope,
                    },
                    calls,
                )
        except OneIdError as exc:
            logger.warning("oneid.logout_failed", err_code=exc.err_code)
        return None


def get_oneid_adapter() -> OneIdAdapter:
    settings = get_settings()
    if settings.oneid_mode == "mock":
        return MockOneId()
    return RealOneId(settings)
