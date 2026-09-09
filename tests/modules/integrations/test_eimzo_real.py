"""The live E-IMZO client against `httpx.MockTransport` — never the real
`e-imzo-server` (the same rule `test_oneid_adapter.py` follows for OneID).

The vendor response shapes asserted here come from Task 2's own fixtures
(`tests/modules/integrations/eimzo_samples.py`), verified against
e-imzo-server v2.1.1's own README and jar (that module's own docstring) — this
file writes no response body of its own.
"""

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.modules.integrations.adapters.eimzo import (
    EimzoError,
    MockEimzo,
    RealEimzo,
    get_eimzo_adapter,
)
from tests.modules.integrations.eimzo_samples import (
    VENDOR_ATTACHED_SAMPLE,
    VENDOR_AUTH_SAMPLE,
    VENDOR_DETACHED_SAMPLE,
)

SETTINGS = Settings(
    eimzo_mode="real",
    # `eimzo_base_url` keeps its private-network default (`http://eimzo:8080`,
    # a bare Docker Compose service name) — only the Host binding needs to be
    # named explicitly for `eimzo_mode=real` to construct at all.
    eimzo_site_host="admin.ruxsatnoma-urmon.uz",
    _env_file=None,  # pyright: ignore[reportCallIssue]
)


def _json_transport(body: dict[str, Any], status_code: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return httpx.MockTransport(handler)


def _adapter(handler) -> RealEimzo:
    return RealEimzo(SETTINGS, transport=httpx.MockTransport(handler))


async def test_verify_detached_sends_document_and_signature_separated_by_a_pipe() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        captured["host"] = request.headers["Host"]
        captured["real_ip"] = request.headers["X-Real-IP"]
        return httpx.Response(200, json=VENDOR_DETACHED_SAMPLE)

    adapter = _adapter(handler)
    result = await adapter.verify_detached(document=b"hello", pkcs7="UEtDUzc=", ip="91.0.0.7")

    assert captured["body"] == f"{base64.b64encode(b'hello').decode()}|UEtDUzc="
    assert captured["host"] == "admin.ruxsatnoma-urmon.uz"
    assert captured["real_ip"] == "91.0.0.7"
    assert result.status_code == 1


async def test_a_provider_refusal_is_a_verdict_not_an_exception() -> None:
    adapter = RealEimzo(SETTINGS, transport=_json_transport({"status": -10, "message": "bad"}))
    result = await adapter.verify_detached(document=b"x", pkcs7="y", ip=None)
    assert result.status_code == -10
    assert result.subject_certificate is None


async def test_transport_failure_raises_err_int_001() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    adapter = _adapter(boom)
    with pytest.raises(EimzoError) as excinfo:
        await adapter.verify_attached("y", ip=None)
    assert excinfo.value.err_code == "ERR-INT-001"


async def test_every_round_trip_is_recorded_for_the_integration_log() -> None:
    adapter = RealEimzo(SETTINGS, transport=_json_transport(VENDOR_AUTH_SAMPLE))
    await adapter.verify_signed_challenge("cGtjczc=", ip="91.0.0.7")
    (call,) = adapter.calls
    assert call.endpoint == "/backend/auth"
    assert call.http_status == 200
    assert call.provider_status == 1


async def test_a_non_200_response_raises_err_int_002() -> None:
    adapter = RealEimzo(SETTINGS, transport=_json_transport({"error": "boom"}, status_code=500))
    with pytest.raises(EimzoError) as excinfo:
        await adapter.verify_attached("y", ip=None)
    assert excinfo.value.err_code == "ERR-INT-002"
    # No `status` field in this body at all — nothing to carry.
    assert excinfo.value.provider_status is None
    assert excinfo.value.reason is None


async def test_a_non_200_response_with_a_status_field_carries_it_on_the_exception() -> None:
    """Finding 4 (fix round 1): a non-200 HTTP response can still carry the
    vendor's own JSON body (`_send`'s own comment on reading the body before
    deciding) — its `status` and mapped reason must survive onto the raised
    exception, not just into the `EimzoCall` log entry."""
    adapter = RealEimzo(
        SETTINGS, transport=_json_transport({"status": -11, "message": "bad cert"}, status_code=502)
    )
    with pytest.raises(EimzoError) as excinfo:
        await adapter.verify_attached("y", ip=None)
    assert excinfo.value.err_code == "ERR-INT-002"
    assert excinfo.value.provider_status == -11
    assert excinfo.value.reason == "certificate_invalid"


async def test_verify_attached_parses_the_vendor_sample() -> None:
    adapter = RealEimzo(SETTINGS, transport=_json_transport(VENDOR_ATTACHED_SAMPLE))
    result = await adapter.verify_attached("some-pkcs7", ip="1.2.3.4")
    assert result.status_code == 1
    assert result.subject_certificate is not None
    assert result.subject_certificate.serial_number == "218712ed3"
    assert result.timestamp_token is not None


async def test_verify_signed_challenge_builds_identity_from_subject_certificate_info() -> None:
    adapter = RealEimzo(SETTINGS, transport=_json_transport(VENDOR_AUTH_SAMPLE))
    identity = await adapter.verify_signed_challenge("cGtjczc=", ip="91.0.0.7")
    assert identity.pinfl == "12345678901234"
    assert identity.full_name == "TESTOV TEST TESTOVICH"
    assert identity.cert_serial == "218712ed3"
    assert identity.cert_expires_at == datetime(2026, 6, 24, 15, 47, 22, tzinfo=UTC)
    # The wire carries no challenge field to echo back — see
    # `RealEimzo.verify_signed_challenge`'s own docstring for why this is
    # deliberately empty rather than invented.
    assert identity.challenge == ""


async def test_verify_signed_challenge_keeps_the_organization_out_of_full_name() -> None:
    """A legal-entity certificate's `subjectName` carries an `O` alongside the
    `CN` — `full_name` must stay just the person's own name (it is written
    straight into `users.full_name`), never `read_subject`'s combined display
    string. `tin`/`legal_name` pick up the organization for `_verify_org_challenge`'s
    two callers, which a plain login never reads."""
    sample = {
        "subjectCertificateInfo": {
            "serialNumber": "org-cert-1",
            "X500Name": "CN=ALIYEV ALI ALIYEVICH,O=BURCHMULLA LESHOZ",
            "subjectName": {
                "1.2.860.3.16.1.2": "31234567890123",
                "1.2.860.3.16.1.1": "301234567",
                "CN": "ALIYEV ALI ALIYEVICH",
                "O": "BURCHMULLA LESHOZ",
            },
            "validFrom": "2026-05-25 15:47:22",
            "validTo": "2026-06-24 15:47:22",
        },
        "status": 1,
        "message": "",
    }
    adapter = RealEimzo(SETTINGS, transport=_json_transport(sample))
    identity = await adapter.verify_signed_challenge("x", ip=None)
    assert identity.pinfl == "31234567890123"
    assert identity.full_name == "ALIYEV ALI ALIYEVICH"
    assert identity.tin == "301234567"
    assert identity.legal_name == "BURCHMULLA LESHOZ"


async def test_verify_signed_challenge_refuses_a_non_1_status() -> None:
    adapter = RealEimzo(SETTINGS, transport=_json_transport({"status": -20, "message": "expired"}))
    with pytest.raises(EimzoError) as excinfo:
        await adapter.verify_signed_challenge("x", ip=None)
    assert excinfo.value.err_code == "ERR-AUTH-004"


async def test_issue_challenge_returns_the_challenge_field() -> None:
    adapter = RealEimzo(
        SETTINGS, transport=_json_transport({"challenge": "abc123", "ttl": 120, "status": 1})
    )
    challenge = await adapter.issue_challenge()
    assert challenge == "abc123"


async def test_issue_challenge_refuses_a_bad_response() -> None:
    adapter = RealEimzo(SETTINGS, transport=_json_transport({"status": -1}))
    with pytest.raises(EimzoError) as excinfo:
        await adapter.issue_challenge()
    assert excinfo.value.err_code == "ERR-INT-002"


async def test_issue_challenge_refusal_carries_the_provider_status_and_reason() -> None:
    """Finding 4 (fix round 1): the vendor's own `status` (`-1`, the
    `EIMZO_STATUS_REASONS` domain) used to be logged and then discarded — a
    route catching `EimzoError` could only answer a bare 502."""
    adapter = RealEimzo(SETTINGS, transport=_json_transport({"status": -1}))
    with pytest.raises(EimzoError) as excinfo:
        await adapter.issue_challenge()
    assert excinfo.value.provider_status == -1
    assert excinfo.value.reason == "certificate_status_unknown"


async def test_attach_timestamp_returns_pkcs7b64() -> None:
    adapter = RealEimzo(
        SETTINGS, transport=_json_transport({"status": 1, "pkcs7b64": "widened-pkcs7"})
    )
    stamped = await adapter.attach_timestamp("pkcs7", ip="1.1.1.1")
    assert stamped == "widened-pkcs7"


async def test_attach_timestamp_refuses_a_non_1_status() -> None:
    adapter = RealEimzo(SETTINGS, transport=_json_transport({"status": -21}))
    with pytest.raises(EimzoError) as excinfo:
        await adapter.attach_timestamp("pkcs7", ip=None)
    assert excinfo.value.err_code == "ERR-INT-002"


async def test_attach_timestamp_refusal_carries_the_provider_status_and_reason() -> None:
    """Finding 4 (fix round 1): Task 7 proxies this call to a citizen's
    browser and needs more than a bare 502 to explain a timestamp refusal."""
    adapter = RealEimzo(SETTINGS, transport=_json_transport({"status": -21}))
    with pytest.raises(EimzoError) as excinfo:
        await adapter.attach_timestamp("pkcs7", ip=None)
    assert excinfo.value.provider_status == -21
    assert excinfo.value.reason == "timestamp_signature_invalid"


async def test_certificate_status_makes_no_provider_call() -> None:
    """Plan 05.2 R3 (option «а», controller ruling T3-1): a date-only verdict,
    never a round trip — the provider has no endpoint that could answer this
    for a bare serial number (`certificate_status`'s own docstring)."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("certificate_status must not call the provider")

    adapter = _adapter(boom)
    future = datetime.now(UTC) + timedelta(days=1)
    past = datetime.now(UTC) - timedelta(days=1)
    assert await adapter.certificate_status("SER-1", "ISS-1", valid_to=future) == "active"
    assert await adapter.certificate_status("SER-1", "ISS-1", valid_to=past) == "expired"
    assert adapter.calls == []


async def test_health_combines_ping_and_info() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ping":
            return httpx.Response(200, json={"status": 1})
        return httpx.Response(200, json={"version": "2.1.1"})

    adapter = _adapter(handler)
    health = await adapter.health()
    assert health == {"ping": {"status": 1}, "info": {"version": "2.1.1"}}
    assert [c.endpoint for c in adapter.calls] == ["/ping", "/info"]


def test_revocation_checkable_is_false_on_real_and_true_on_mock() -> None:
    # PF3: `signatures.reverify()` (Task 6) reads this to report which check
    # it actually made without asking which adapter class it holds.
    real = RealEimzo(SETTINGS, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert real.revocation_checkable is False
    assert MockEimzo().revocation_checkable is True


def test_the_factory_returns_the_real_adapter_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("EIMZO_MODE", "real")
    monkeypatch.setenv("EIMZO_SITE_HOST", "admin.ruxsatnoma-urmon.uz")
    get_settings.cache_clear()
    try:
        assert isinstance(get_eimzo_adapter(), RealEimzo)
    finally:
        get_settings.cache_clear()
