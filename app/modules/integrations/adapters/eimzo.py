"""E-IMZO adapter seam. The mock treats a base64url JSON envelope as a valid
signature or certificate (ruling 3), for both the login challenge and document
verification; `RealEimzo` (stage 5.2 task 3) speaks the live e-imzo-server
v2.1.1 protocol over the stack's private network — `/backend/auth`,
`/backend/pkcs7/verify/{attached,detached}`, `/frontend/challenge`,
`/frontend/timestamp/pkcs7`, `/ping`, `/info` — using Task 2's wire parsers
(`eimzo_wire.py`).
A legal-entity certificate carries the org STIR in `tin` (login) or
`pinfl_or_stir` (documents) alongside the signer's pinfl."""

import base64
import hashlib
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import httpx
import structlog

from app.config import Settings, get_settings
from app.core.security import new_token
from app.modules.integrations.adapters.mock_codec import decode_payload, encode_payload

# `eimzo_wire.py` imports `EimzoCertificateInfo`/`EimzoVerification` FROM this
# module at its own top level (Task 2), so a module-level import in the other
# direction here would be a real circular import, fragile to whichever of the
# two modules happens to load first (the same shape `permits/service.py`
# already works around for `decisions.py`). `RealEimzo`'s methods import
# `eimzo_wire` locally instead — cost-free once both modules have finished
# loading, and it creates no cycle.

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class EimzoIdentity:
    challenge: str
    pinfl: str
    full_name: str
    tin: str | None = None
    legal_name: str | None = None
    cert_serial: str = "MOCK-CERT"
    cert_expires_at: datetime | None = None

    def to_payload(self) -> dict[str, Any]:
        data = asdict(self)
        if self.cert_expires_at is not None:
            data["cert_expires_at"] = self.cert_expires_at.isoformat()
        return data

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> EimzoIdentity:
        expires = data.get("cert_expires_at")
        parsed = datetime.fromisoformat(expires) if isinstance(expires, str) else None
        return cls(**{**data, "cert_expires_at": parsed})


class EimzoError(Exception):
    """Provider unreachable, refused, or answered with a non-1 status.

    `err_code` picks the response a caller maps this to (`ERR-INT-001`/
    `ERR-INT-002`/`ERR-AUTH-004`/...). `provider_status`/`reason` (fix round
    1, finding 4) carry the PROVIDER's own numeric status — the
    `EIMZO_STATUS_REASONS` domain, e.g. `-10`/`-20` — and its mapped
    machine-readable reason, so a route catching this can answer with more
    than a bare 502/503 (stage 3.8 ruling 9; Task 7 needs this to explain a
    timestamp refusal to the browser). Deliberately carries nothing else: no
    PKCS#7, no PINFL, no other personal data belongs on an exception a route
    might echo straight into a response body."""

    def __init__(
        self,
        err_code: str = "ERR-AUTH-004",
        *,
        provider_status: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(err_code)
        self.err_code = err_code
        self.provider_status = provider_status
        self.reason = reason


# design/04 §2.5 — the seven E-IMZO status codes. `1` is success; every other
# code must keep its own machine-readable reason (ruling 9) rather than
# collapsing into one "signature error".
#
# Stage 5.2 adds four more, specific to `/backend/pkcs7/verify/{attached,
# detached}` (github.com/qo0p/e-imzo-doc): `0` is the VENDOR's own reserved
# code, not this codebase's — the README documents it for `/frontend/mobile`
# as "bad response, should never happen"; it is not spelled out again for
# the pkcs7-verify endpoints, but it is the same reserved value, and
# `eimzo_wire.verification_from_pkcs7_info` falls back to it only when the
# provider's response carries no `status` field at all (exactly the
# "should never happen" shape). `-21`/`-22`/`-23` are the provider's own
# timestamp-specific failures, distinct from the `-10`/`-11`/`-12`
# signature/certificate codes above even though the wording is almost the
# same.
EIMZO_STATUS_REASONS: dict[int, str] = {
    1: "ok",
    0: "provider_bad_response",
    -1: "certificate_status_unknown",
    -5: "clock_skew",
    -10: "signature_invalid",
    -11: "certificate_invalid",
    -12: "certificate_invalid_at_signing",
    -20: "challenge_expired",
    -21: "timestamp_signature_invalid",
    -22: "timestamp_certificate_invalid",
    -23: "timestamp_certificate_invalid_at_signing",
}

CertificateStatus = Literal["active", "revoked", "expired"]


@dataclass(frozen=True)
class EimzoCertificateInfo:
    serial_number: str
    issuer: str
    subject: str
    pinfl_or_stir: str
    valid_from: datetime
    valid_to: datetime


@dataclass(frozen=True)
class EimzoVerification:
    status_code: int
    subject_certificate: EimzoCertificateInfo | None
    signed_at: datetime | None
    # Real mode: the trusted timestamp's own ISO time string, not a
    # cryptographic token — the wire has no separate token field to carry
    # (see `eimzo_wire.verification_from_pkcs7_info`).
    timestamp_token: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class EimzoCall:
    """One provider round trip, for `integration_log` (mirrors `OneIdCall`,
    `oneid.py`). Carries no personal data and no signed document — only which
    endpoint it was, how it went, and the provider's own status/message when
    it answered at all. Appended to `RealEimzo.calls` by `_post`/`_get`
    WHATEVER HAPPENS (transport failure, a non-200, or a normal reply), so a
    refusal is exactly as loggable as a success."""

    endpoint: str
    http_status: int | None
    duration_ms: int
    provider_status: int | None = None
    provider_message: str | None = None


class EimzoAdapter(Protocol):
    # Stage 5.2 task 3: `True` when the adapter can independently discover a
    # revocation (`MockEimzo`, whose serial-prefix convention answers
    # instantly); `False` on `RealEimzo`, which has no such endpoint at all
    # (see `RealEimzo.certificate_status`'s own docstring for why). Task 6
    # reads this so `reverify()` can report which check it actually made
    # without asking which adapter class it holds — `signatures` is a level-2
    # module and must not know that (PF3).
    revocation_checkable: bool

    async def verify_signed_challenge(
        self, signed_challenge: str, ip: str | None
    ) -> EimzoIdentity: ...

    async def issue_challenge(self, ip: str | None) -> str: ...

    async def verify_attached(self, pkcs7: str, ip: str | None) -> EimzoVerification: ...

    async def verify_detached(
        self, document: bytes, pkcs7: str, ip: str | None
    ) -> EimzoVerification: ...

    async def attach_timestamp(self, pkcs7: str, ip: str | None) -> str: ...

    async def certificate_status(
        self, serial: str, issuer: str, valid_to: datetime
    ) -> CertificateStatus: ...

    async def health(self) -> dict[str, Any]: ...


def encode_mock_signed_challenge(identity: EimzoIdentity) -> str:
    return encode_payload(identity.to_payload())


def encode_mock_signature(
    document: bytes,
    serial: str,
    issuer: str,
    pinfl: str,
    *,
    subject: str | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    signed_at: datetime | None = None,
    timestamp_token: str | None = "MOCK-TS",
) -> str:
    """Dev/test helper: a PKCS#7 stand-in over `document`, same base64url-JSON
    codec as the login mock (ruling 3). Carries the certificate fields, the
    signing time, the document itself (so `verify_attached` can recover it) and
    its sha256 (so `verify_detached` can check it without decoding the rest).
    Unset validity/signing times default to a window that brackets "now", so a
    caller that only names the four required fields still gets a valid signature."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "serial_number": serial,
        "issuer": issuer,
        "subject": subject or f"PINFL={pinfl}",
        "pinfl_or_stir": pinfl,
        "valid_from": (valid_from or now - timedelta(days=30)).isoformat(),
        "valid_to": (valid_to or now + timedelta(days=365)).isoformat(),
        "signed_at": (signed_at or now).isoformat(),
        "timestamp_token": timestamp_token,
        "document_b64": base64.b64encode(document).decode(),
        "document_sha256": hashlib.sha256(document).hexdigest(),
    }
    return encode_payload(payload)


def _unparseable_signature() -> EimzoVerification:
    return EimzoVerification(
        status_code=-10, subject_certificate=None, signed_at=None, timestamp_token=None, raw={}
    )


def _verify_envelope(pkcs7: str, document: bytes | None) -> EimzoVerification:
    """Shared verdict for verify_attached/verify_detached: decode the mock
    envelope and check the sha256 recorded at encoding time against the bytes
    that were actually signed. `document` is the caller-supplied bytes for the
    detached case; None means "recover them from the envelope" (attached).
    Any malformed input maps to -10 rather than raising — a verify call's job
    is to return a verdict, not to throw (unlike verify_signed_challenge, whose
    EimzoError is part of the existing, unchanged login contract)."""
    try:
        data = decode_payload(pkcs7)
        signed_bytes = document if document is not None else base64.b64decode(data["document_b64"])
        cert = EimzoCertificateInfo(
            serial_number=data["serial_number"],
            issuer=data["issuer"],
            subject=data["subject"],
            pinfl_or_stir=data["pinfl_or_stir"],
            valid_from=datetime.fromisoformat(data["valid_from"]),
            valid_to=datetime.fromisoformat(data["valid_to"]),
        )
        signed_at = datetime.fromisoformat(data["signed_at"])
    # Deliberately parenthesized, not the PEP 758 bare form (see
    # core.settings_store.coerce for the reasoning). `binascii.Error` is a
    # `ValueError` subclass already, so it needs no separate entry here.
    except (ValueError, TypeError, KeyError):  # fmt: skip
        return _unparseable_signature()
    matches = hashlib.sha256(signed_bytes).hexdigest() == data.get("document_sha256")
    return EimzoVerification(
        status_code=1 if matches else -10,
        subject_certificate=cert,
        signed_at=signed_at,
        timestamp_token=data.get("timestamp_token"),
        # Fix wave: `document_b64` exists so THIS function can recover the
        # attached document above -- it has no business surviving into the
        # evidence a caller stores. `signatures.verify.build_verdict` copies
        # `raw` verbatim into `record["raw"]`, and `service.sign()` stores
        # that record whole in the append-only `signatures.verification`
        # column, returned in full to every co-signer through
        # `GET /signatures` -- so leaving the document's own bytes in here
        # would silently contradict this module's own contract ("this module
        # never stores the original document bytes", `verify.py`'s docstring
        # and `reverify`'s) and roughly 4/3 the size of every future permit
        # PDF into that table, per signature. Everything else in `data`
        # (the certificate identity, `document_sha256`, the timestamp token)
        # stays -- only this one key is adapter-internal plumbing.
        raw={k: v for k, v in data.items() if k != "document_b64"},
    )


class MockEimzo:
    # PF3: the mock's serial-prefix convention (`certificate_status` below)
    # answers a revocation question directly and instantly — unlike
    # `RealEimzo`, which has no endpoint that can (see its own docstring).
    revocation_checkable: bool = True

    async def verify_signed_challenge(self, signed_challenge: str, ip: str | None) -> EimzoIdentity:
        try:
            return EimzoIdentity.from_payload(decode_payload(signed_challenge))
        except (ValueError, TypeError) as exc:
            raise EimzoError() from exc

    async def issue_challenge(self, ip: str | None) -> str:
        """Ruling 11: in the real protocol the challenge belongs to e-imzo-server
        (`/frontend/challenge`), not to us. The mock only needs an opaque token in
        the same shape; `ip` is accepted for parity with `RealEimzo` and ignored
        — the mock has no provider round trip to attach it to. `auth.service.
        issue_eimzo_challenge` calls THIS method only in `real` mode: in `mock`
        mode (still the default) it keeps minting and storing its own token in
        `otp_codes` exactly as before ruling 11, so this method is never reached
        from a mock-mode login at all."""
        return new_token()

    async def verify_attached(self, pkcs7: str, ip: str | None) -> EimzoVerification:
        return _verify_envelope(pkcs7, None)

    async def verify_detached(
        self, document: bytes, pkcs7: str, ip: str | None
    ) -> EimzoVerification:
        return _verify_envelope(pkcs7, document)

    async def attach_timestamp(self, pkcs7: str, ip: str | None) -> str:
        """No real trusted-timestamp authority to call: the mock's own envelope
        (`encode_mock_signature`) already carries a `timestamp_token`, so there
        is nothing more for this to attach — it echoes the same envelope back
        unchanged, the same "nothing to do" shape `RealOneId.logout` uses when
        there is no token to act on."""
        return pkcs7

    async def certificate_status(
        self, serial: str, issuer: str, valid_to: datetime
    ) -> CertificateStatus:
        """`issuer` is accepted for parity with a real lookup (a serial number is
        only unique within its issuing CA) but unused by the mock: status is
        decided by a serial prefix convention — `REVOKED-` for `revoked`,
        `EXPIRED-` for `expired` (mirroring `REVOKED-`; design/04 §2.5 has no
        separate "expired" status code because a real CRL/OCSP check reports it
        directly), anything else is `active`. `valid_to` (ruling T3-1) is
        accepted for parity with `RealEimzo` and ignored — the prefix
        convention already answers "expired" on its own, and it predates this
        parameter."""
        if serial.startswith("REVOKED-"):
            return "revoked"
        if serial.startswith("EXPIRED-"):
            return "expired"
        return "active"

    async def health(self) -> dict[str, Any]:
        return {"ping": "mock", "info": {"mode": "mock"}}


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _body_status(body: dict[str, Any] | None) -> int | None:
    """The vendor's own `status` field, kept only when it is actually an
    `int` (`EIMZO_STATUS_REASONS`'s domain) — a malformed or absent field
    must not crash the refusal it is trying to explain (finding 4)."""
    status = body.get("status") if body else None
    return status if isinstance(status, int) else None


class RealEimzo:
    """Live `e-imzo-server` v2.1.1 client (design/04 §2, plan 05.2), reached
    over the stack's private network — `Settings._forbid_default_secret_in_prod`
    already refuses `eimzo_mode=real` with a publicly-reachable
    `eimzo_base_url` (plan 05.2 R2), so this class never has to defend that
    boundary itself.

    A fresh `httpx.AsyncClient` per call, the same reasoning `RealOneId` gives
    for its own: a pooled client bound to one event loop breaks in a process
    that also runs workers. No retries — a citizen or an official is watching
    a browser, and a retry loop during a provider outage only spends more of
    their patience for the same answer.

    Every call carries `X-Real-IP` (the SIGNER's own address — not this
    server's) and `Host` (`settings.eimzo_site_host`, the domain the API-KEY
    is bound to, decision #165) — e-imzo-server holds no auth of its own
    beyond network placement and that Host binding.

    `self.calls` accumulates one `EimzoCall` per round trip made through THIS
    instance, WHATEVER HAPPENS (success, a provider refusal, or a transport
    failure) — `get_eimzo_adapter()` builds a fresh instance per call site, so
    everything a single `sign()`/`register_certificate()`/login attempt did
    against the provider sits in one list Task 5 can drain into
    `integration_log` in one go, the same shape `RealOneId.calls` serves
    `exchange_code`, just kept on the adapter instance instead of returned
    per-call: `signatures.service.sign()` calls `verify_detached` and
    `certificate_status` on the SAME adapter object, and only the first of
    those is a real round trip."""

    # PF3: no endpoint answers "is this certificate revoked" for a bare
    # serial number at all (see `certificate_status`'s own docstring) — the
    # opposite of `MockEimzo`'s instant prefix convention.
    revocation_checkable: bool = False

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport  # tests inject httpx.MockTransport
        self.calls: list[EimzoCall] = []

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._settings.eimzo_base_url,
            timeout=self._settings.eimzo_timeout_seconds,
            transport=self._transport,
        )

    def _headers(self, ip: str | None) -> dict[str, str]:
        # `ip` is `None` whenever the caller has no signer address to report
        # (`issue_challenge`, `health`, or a caller that itself received
        # `None` — `request.client` can be unset) — httpx headers must be
        # strings, so that becomes an empty value rather than a crash.
        return {"X-Real-IP": ip or "", "Host": self._settings.eimzo_site_host}

    async def _send(
        self, method: str, path: str, *, content: str | None, ip: str | None
    ) -> dict[str, Any]:
        started = time.monotonic()
        try:
            async with self._client() as client:
                response = await client.request(
                    method, path, content=content, headers=self._headers(ip)
                )
        except httpx.HTTPError as exc:
            self.calls.append(EimzoCall(path, None, _ms(started)))
            logger.warning("eimzo.transport_error", endpoint=path, error=type(exc).__name__)
            raise EimzoError("ERR-INT-001") from None

        # Read the body before deciding, same reasoning as `RealOneId._post`:
        # a refusal this codebase can act on may still arrive with a JSON
        # body worth logging even on a non-200 response.
        try:
            payload = response.json()
        except ValueError:
            payload = None
        body = payload if isinstance(payload, dict) else None
        self.calls.append(
            EimzoCall(
                path,
                response.status_code,
                _ms(started),
                provider_status=_body_status(body),
                provider_message=str(body["message"]) if body and body.get("message") else None,
            )
        )
        if response.status_code != 200:
            logger.warning("eimzo.provider_unavailable", endpoint=path, status=response.status_code)
            # `_body_status`/`EIMZO_STATUS_REASONS` (finding 4): a non-200 can
            # still carry the vendor's own JSON body (the comment above is
            # why it was already parsed) — its `status` and mapped reason
            # survive onto the exception, not only into the `EimzoCall` log
            # entry `self.calls` already got a few lines up.
            provider_status = _body_status(body)
            raise EimzoError(
                "ERR-INT-002",
                provider_status=provider_status,
                reason=EIMZO_STATUS_REASONS.get(provider_status)
                if provider_status is not None
                else None,
            )
        if body is None:
            raise EimzoError("ERR-INT-002")
        return body

    async def _post(self, path: str, body: str, *, ip: str | None) -> dict[str, Any]:
        return await self._send("POST", path, content=body, ip=ip)

    async def _get(self, path: str) -> dict[str, Any]:
        return await self._send("GET", path, content=None, ip=None)

    async def issue_challenge(self, ip: str | None) -> str:
        """`POST /frontend/challenge` (design/04 §2.2 step 2): the challenge
        belongs to e-imzo-server here, with its own 120-second TTL — unlike
        the mock's opaque token, this one must be handed to the browser's
        `create_pkcs7` call and back to `/backend/auth` before it expires.
        `auth.service.issue_eimzo_challenge` calls this in `real` mode
        (ruling 11) and threads through the CITIZEN's own address so it
        reaches `X-Real-IP` here, same as every other provider-reaching
        call — there is no session yet at this point, so `ip` comes straight
        from the request, not from an authenticated actor."""
        payload = await self._post("/frontend/challenge", "", ip=ip)
        challenge = payload.get("challenge")
        status = _body_status(payload)
        if status != 1 or not isinstance(challenge, str):
            logger.warning("eimzo.challenge_refused", status=payload.get("status"))
            raise EimzoError(
                "ERR-INT-002",
                provider_status=status,
                reason=EIMZO_STATUS_REASONS.get(status) if status is not None else None,
            )
        return challenge

    async def verify_signed_challenge(self, signed_challenge: str, ip: str | None) -> EimzoIdentity:
        """`POST /backend/auth` (design/04 §2.2 step 4): e-imzo-server itself
        verifies the signature, matches the challenge it remembers and checks
        certificate status over the VPN before ever answering `status: 1` —
        by the time this reads `subjectCertificateInfo`, all three have
        already happened at the provider. A non-1 status is this method's own
        exception (`EimzoError`, `ERR-AUTH-004`), unlike the verify family
        below — the SAME unchanged login contract `MockEimzo`'s own docstring
        on `_verify_envelope` already documents.

        **`EimzoIdentity.challenge` is deliberately left `""` here.** The
        response carries no challenge field to echo back (design/04 §2.2 step
        5 lists only `subjectCertificateInfo` and `status`) — e-imzo-server's
        OWN challenge, matched above, is not the same value our
        `auth.service.issue_eimzo_challenge` stores in `otp_codes` (ruling 11
        again: that redirection has not happened yet). Reconciling the two
        challenge stores is the later task ruling 11 already names, not this
        one; this method stays honest about what the wire actually returns
        rather than inventing a value that would look right in a debugger and
        wrong in production.

        **`full_name` is the certificate's `CN` alone, never `read_subject`'s
        combined `subject` string.** That combined form (`"CN, O"`) is right
        for `EimzoCertificateInfo.subject` — a display field for a signature
        viewer — but `login_or_create_by_pinfl` writes this value straight
        into `users.full_name`; joining an organization's name onto it on
        every legal-entity-certificate login would corrupt a real citizen's
        name field. `tin`/`legal_name` (the org's own STIR and display name)
        are filled in from the same `subjectName` map for `_verify_org_challenge`'s
        two callers, which `login_via_eimzo` itself never reads.

        Local import of `eimzo_wire` (module docstring): that module imports
        FROM this one at its own top level, so importing it back at THIS
        module's top level would be a real circular import."""
        from app.modules.integrations.adapters import eimzo_wire

        payload = await self._post("/backend/auth", signed_challenge, ip=ip)
        status = payload.get("status", 0)
        if status != 1:
            logger.warning(
                "eimzo.auth_refused",
                status=status,
                reason=EIMZO_STATUS_REASONS.get(status, "unknown"),
            )
            raise EimzoError("ERR-AUTH-004")
        info = payload.get("subjectCertificateInfo")
        if not isinstance(info, dict):
            raise EimzoError("ERR-AUTH-004")
        subject_name = info.get("subjectName") or {}
        _display, identifier, legal_tin = eimzo_wire.read_subject(subject_name)
        full_name = str(subject_name.get("CN") or "").strip()
        legal_name = str(subject_name.get("O") or "").strip() or None
        valid_to = info.get("validTo")
        return EimzoIdentity(
            challenge="",
            pinfl=identifier,
            full_name=full_name,
            tin=legal_tin,
            legal_name=legal_name,
            cert_serial=str(info.get("serialNumber", "")),
            cert_expires_at=eimzo_wire.parse_provider_datetime(valid_to) if valid_to else None,
        )

    async def verify_attached(self, pkcs7: str, ip: str | None) -> EimzoVerification:
        """`POST /backend/pkcs7/verify/attached`: the body IS the pkcs7
        (`Pkcs7InfoJson.documentBase64` carries the document back, per Task
        2's own sample). A non-1 `status` in the parsed response is a
        VERDICT, not an exception — `eimzo.py::_verify_envelope`'s contract,
        unchanged here: only a transport failure or a non-200 HTTP response
        raises."""
        from app.modules.integrations.adapters import eimzo_wire

        payload = await self._post("/backend/pkcs7/verify/attached", pkcs7, ip=ip)
        return eimzo_wire.verification_from_pkcs7_info(payload)

    async def verify_detached(
        self, document: bytes, pkcs7: str, ip: str | None
    ) -> EimzoVerification:
        """`POST /backend/pkcs7/verify/detached`: body is
        `base64(document)|pkcs7` (README, Task 2's own sample) — the caller
        already holds the document, so nothing echoes it back. Same verdict
        contract as `verify_attached` above."""
        from app.modules.integrations.adapters import eimzo_wire

        body = f"{base64.b64encode(document).decode()}|{pkcs7}"
        payload = await self._post("/backend/pkcs7/verify/detached", body, ip=ip)
        return eimzo_wire.verification_from_pkcs7_info(payload)

    async def attach_timestamp(self, pkcs7: str, ip: str | None) -> str:
        """`POST /frontend/timestamp/pkcs7`: attaches a trusted timestamp to
        an already-produced signature, returning the widened envelope's own
        `pkcs7b64` field. Task 7 exposes this through a route; this method
        only provides it. A non-1 status here is NOT a verdict the way the
        verify family's is — there is no partial "timestamp invalid" result a
        caller could act on, only a signature that either gained a timestamp
        or did not — so it raises the same as any other bad response."""
        payload = await self._post("/frontend/timestamp/pkcs7", pkcs7, ip=ip)
        stamped = payload.get("pkcs7b64")
        status = _body_status(payload)
        if status != 1 or not isinstance(stamped, str):
            logger.warning("eimzo.timestamp_refused", status=payload.get("status"))
            raise EimzoError(
                "ERR-INT-002",
                provider_status=status,
                reason=EIMZO_STATUS_REASONS.get(status) if status is not None else None,
            )
        return stamped

    async def certificate_status(
        self, serial: str, issuer: str, valid_to: datetime
    ) -> CertificateStatus:
        """**No call to the provider at all — this is the design, not a
        shortcut.** `e-imzo-server` has no endpoint that answers "is this
        certificate revoked" for a bare serial number: revocation is checked
        only AS PART OF verifying a signature, over the VPN, inside
        `/backend/auth` and the two `/backend/pkcs7/verify/*` routes — there
        is nothing this method could call. Plan 05.2 R3 (option «а», Oybek,
        2026-09-09) is that in production a revocation is therefore
        discovered at the NEXT signature, never before, and that the system
        must SAY so rather than implying a check it did not make
        (`revocation_checkable = False` above is exactly that signal, read by
        Task 6's `reverify()`). `serial` and `issuer` are accepted only for
        parity with `MockEimzo`'s signature and are unused here — there is no
        lookup to key them against. A later reader tempted to "fix" this into
        a provider call should read this docstring first: that call does not
        exist."""
        return "expired" if valid_to < datetime.now(UTC) else "active"

    async def health(self) -> dict[str, Any]:
        """`GET /ping` and `GET /info`: whatever each says, passed through
        whole under its own key — Task 8 exposes this as an ops route, so
        this method decides nothing about what "healthy" means."""
        ping = await self._get("/ping")
        info = await self._get("/info")
        return {"ping": ping, "info": info}


def get_eimzo_adapter() -> EimzoAdapter:
    settings = get_settings()
    if settings.eimzo_mode == "mock":
        return MockEimzo()
    return RealEimzo(settings)
