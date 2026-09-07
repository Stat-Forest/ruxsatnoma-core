"""The live OneID client against httpx.MockTransport — never the real provider
(the same rule the Eskiz adapter's tests follow).

The refusal bodies asserted here are VERBATIM what `sso.egov.uz` returned to
probes from the dev server on 2026-09-07; they are not invented shapes.
"""

import urllib.parse

import httpx
import pytest

from app.config import Settings
from app.modules.integrations.adapters.oneid import (
    OneIdError,
    RealOneId,
    get_oneid_adapter,
    parse_identify,
)

SETTINGS = Settings(
    oneid_mode="real",
    oneid_client_id="forestry_uz",
    oneid_client_secret="s3cret",
    oneid_scope="forestry_uz",
    oneid_redirect_uri="https://dev-api.ruxsatnoma-urmon.uz/api/v1/auth/oneid/callback",
    oneid_base_url="https://sso.test/sso/oauth/Authorization.do",
    _env_file=None,  # pyright: ignore[reportCallIssue]
)

# The identify response, with the field names the old system's working
# integration actually reads (design/04 §1.3, plan 05.1 R2).
IDENTIFY = {
    "pin": "31234567890123",
    "full_name": "ALIYEV ALI ALIYEVICH",
    "first_name": "ALI",
    "mid_name": "ALIYEV",  # family name
    "sur_name": "ALIYEVICH",  # patronymic
    "birth_date": "1990-01-15",
    "mob_phone_no": "998901234567",
    "pport_no": "AA1234567",
    "auth_method": "LEPKCSMETHOD",
    "pkcs_legal_tin": "307575940",
    "valid": "true",
    "user_id": "aliyev_ali",
    "legal_info": [
        {"is_basic": True, "le_tin": "307575940", "le_name": "OOO TEST"},
        {"is_basic": False, "tin": "200000001", "acron_UZ": "OOO SECOND"},
    ],
}


def _adapter(handler) -> RealOneId:
    return RealOneId(SETTINGS, transport=httpx.MockTransport(handler))


def _body(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(request.content.decode()).items()}


def _token_then_identify(request: httpx.Request) -> httpx.Response:
    if _body(request)["grant_type"] == "one_authorization_code":
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    return httpx.Response(200, json=IDENTIFY)


def test_authorize_url_is_the_documented_shape():
    url = _adapter(_token_then_identify).authorize_url(
        state="st", redirect_uri=SETTINGS.oneid_redirect_uri, scope="forestry_uz"
    )
    assert url.startswith("https://sso.test/sso/oauth/Authorization.do?")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["response_type"] == ["one_code"]
    assert query["client_id"] == ["forestry_uz"]
    assert query["scope"] == ["forestry_uz"]
    assert query["state"] == ["st"]
    assert query["redirect_uri"] == [SETTINGS.oneid_redirect_uri]


async def test_exchange_posts_both_grants_and_maps_the_profile():
    grants: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _body(request)
        grants.append(body["grant_type"])
        if body["grant_type"] == "one_authorization_code":
            assert body["code"] == "the-code"
            assert body["client_secret"] == "s3cret"
            assert body["redirect_uri"] == SETTINGS.oneid_redirect_uri
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        assert body["access_token"] == "tok-1"
        assert body["scope"] == "forestry_uz"
        return httpx.Response(200, json=IDENTIFY)

    login = await _adapter(handler).exchange_code("the-code")

    assert grants == ["one_authorization_code", "one_access_token_identify"]
    assert login.access_token == "tok-1"
    assert login.profile.pinfl == "31234567890123"
    # The naming trap, asserted rather than merely commented (design/04 §1.3):
    assert login.profile.mid_name == "ALIYEV"  # family name
    assert login.profile.sur_name == "ALIYEVICH"  # patronymic
    assert login.profile.phone == "+998901234567"  # normalized to our own format
    assert login.profile.passport == "AA1234567"
    assert login.profile.valid is True
    assert login.profile.auth_method == "LEPKCSMETHOD"
    assert login.profile.pkcs_legal_tin == "307575940"
    assert [li.le_tin for li in login.profile.legal_info] == ["307575940", "200000001"]
    assert login.profile.legal_info[0].is_basic is True
    assert login.profile.legal_info[1].le_name == "OOO SECOND"  # acron_UZ fallback
    assert [c.endpoint for c in login.calls] == [
        "one_authorization_code",
        "one_access_token_identify",
    ]
    assert all(c.http_status == 200 for c in login.calls)


async def test_transport_failure_is_an_outage():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(OneIdError) as excinfo:
        await _adapter(handler).exchange_code("the-code")
    assert excinfo.value.err_code == "ERR-INT-001"
    assert excinfo.value.calls  # the failed attempt is still loggable
    assert excinfo.value.calls[0].http_status is None


async def test_a_server_error_is_an_outage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(OneIdError) as excinfo:
        await _adapter(handler).exchange_code("the-code")
    assert excinfo.value.err_code == "ERR-INT-001"


async def test_a_wrong_client_secret_is_a_refusal_not_an_outage():
    """Plan 05.1 R7, and the body is verbatim what the live provider returned
    on 2026-09-07 to a POST carrying a wrong secret. Reporting this as
    ERR-INT-001 would tell an administrator to wait out a provider outage that
    is not happening while the fault is one line of our own .env."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "status": 400,
                "message": "ClientSecretException",
                "path": "/sso/oauth/Authorization.do",
                "error": "CLIENT_SECRET_NOT_FOUND",
                "timestamp": 1788786800750,
            },
        )

    with pytest.raises(OneIdError) as excinfo:
        await _adapter(handler).exchange_code("the-code")
    assert excinfo.value.err_code == "ERR-INT-002"
    call = excinfo.value.calls[0]
    assert call.http_status == 400
    assert call.provider_error == "CLIENT_SECRET_NOT_FOUND"
    assert call.provider_message == "ClientSecretException"


async def test_a_200_without_an_access_token_is_a_bad_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "invalid_client"})

    with pytest.raises(OneIdError) as excinfo:
        await _adapter(handler).exchange_code("the-code")
    assert excinfo.value.err_code == "ERR-INT-002"


async def test_a_non_json_body_is_a_bad_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(OneIdError) as excinfo:
        await _adapter(handler).exchange_code("the-code")
    assert excinfo.value.err_code == "ERR-INT-002"


async def test_an_identify_response_without_a_pin_is_a_bad_response():
    """PINFL is our natural key. A profile without it cannot log anybody in,
    and inventing a user from it would create a SECOND account for a citizen
    who already has one."""

    def handler(request: httpx.Request) -> httpx.Response:
        if _body(request)["grant_type"] == "one_authorization_code":
            return httpx.Response(200, json={"access_token": "tok-1"})
        return httpx.Response(200, json={"full_name": "NO PIN HERE"})

    with pytest.raises(OneIdError) as excinfo:
        await _adapter(handler).exchange_code("the-code")
    assert excinfo.value.err_code == "ERR-INT-002"
    # Both round trips still reached the log, including the one that answered
    # a body we could not use.
    assert len(excinfo.value.calls) == 2


def test_the_minimal_field_set_still_parses():
    """Plan 05.1 R6: what arrives today is not a promise about tomorrow, and
    the failure would be silent — an empty autofill is indistinguishable from
    a citizen who typed nothing."""
    profile = parse_identify({"pin": "31234567890123", "full_name": "ALIYEV ALI"})
    assert profile.pinfl == "31234567890123"
    assert profile.phone is None
    assert profile.passport is None
    assert profile.legal_info == ()
    assert profile.valid is None


def test_full_name_falls_back_to_the_parts():
    # users.full_name is NOT NULL; a provider that omits full_name would
    # otherwise write an empty name onto a real citizen's account.
    profile = parse_identify(
        {"pin": "31234567890123", "mid_name": "ALIYEV", "first_name": "ALI", "sur_name": "OTA"}
    )
    assert profile.full_name == "ALIYEV ALI OTA"


def test_the_passport_falls_back_to_doc_num():
    # design/04 §1.3: the TI's field table says `doc_num`, its sample response
    # says `pport_no`, and the old system reads `pport_no`. Accept either.
    assert parse_identify({"pin": "3", "doc_num": "AB7654321"}).passport == "AB7654321"


def test_a_legal_entry_without_a_stir_is_dropped():
    # It cannot be a representation basis, and a blank STIR in the list would
    # render as an empty row in "apply on behalf of an organization".
    profile = parse_identify(
        {"pin": "3", "legal_info": [{"le_name": "NO STIR"}, {"le_tin": "1", "le_name": "OK"}]}
    )
    assert [li.le_tin for li in profile.legal_info] == ["1"]


def test_phone_shapes_are_normalized_or_dropped():
    # Our own format is +998XXXXXXXXX (auth/schemas.py). A number stored in the
    # provider's shape would be displayed, then REJECTED the first time the
    # citizen edits their profile.
    assert parse_identify({"pin": "3", "mob_phone_no": "998901234567"}).phone == "+998901234567"
    assert (
        parse_identify({"pin": "3", "mob_phone_no": "+998 90 123-45-67"}).phone == "+998901234567"
    )
    assert parse_identify({"pin": "3", "mob_phone_no": "901234567"}).phone == "+998901234567"
    assert parse_identify({"pin": "3", "mob_phone_no": "12345"}).phone is None
    assert parse_identify({"pin": "3", "mob_phone_no": ""}).phone is None


def test_valid_accepts_the_providers_string_booleans():
    assert parse_identify({"pin": "3", "valid": "true"}).valid is True
    assert parse_identify({"pin": "3", "valid": True}).valid is True
    assert parse_identify({"pin": "3", "valid": "false"}).valid is False
    assert parse_identify({"pin": "3"}).valid is None


async def test_logout_posts_one_log_out():
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_body(request))
        return httpx.Response(200, json={"status": "ok"})

    await _adapter(handler).logout("tok-1")
    assert seen[0]["grant_type"] == "one_log_out"
    assert seen[0]["access_token"] == "tok-1"
    assert seen[0]["scope"] == "forestry_uz"


async def test_a_failing_logout_never_raises():
    """Our own session is already revoked by the time this runs (task 5), so a
    provider failure must not turn "sign out" into an error for the citizen."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    await _adapter(handler).logout("tok-1")


async def test_logout_without_a_token_makes_no_call():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    await _adapter(handler).logout(None)


def test_the_factory_returns_the_real_adapter_when_configured(monkeypatch):
    from app.config import get_settings

    for name, value in (
        ("ONEID_MODE", "real"),
        ("ONEID_CLIENT_ID", "forestry_uz"),
        ("ONEID_CLIENT_SECRET", "s3cret"),
        ("ONEID_SCOPE", "forestry_uz"),
        ("ONEID_REDIRECT_URI", "https://dev-api.ruxsatnoma-urmon.uz/api/v1/auth/oneid/callback"),
    ):
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        assert isinstance(get_oneid_adapter(), RealOneId)
    finally:
        get_settings.cache_clear()
