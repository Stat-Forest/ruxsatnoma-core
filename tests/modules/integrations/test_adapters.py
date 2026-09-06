"""Adapter seam: mock codecs, factories, prod guard lives in test_config."""

import urllib.parse

import pytest

from app.modules.integrations.adapters.eimzo import (
    EimzoError,
    EimzoIdentity,
    encode_mock_signed_challenge,
    get_eimzo_adapter,
)
from app.modules.integrations.adapters.oneid import (
    MOCK_DEMO_PROFILE,
    MockOneId,
    OneIdError,
    OneIdLegalInfo,
    OneIdProfile,
    encode_mock_code,
    get_oneid_adapter,
    provider_authorize_url,
)
from app.modules.integrations.adapters.otp_sender import MockOtpSender, get_otp_sender

PROFILE = OneIdProfile(
    pinfl="12345678901234",
    full_name="TESTOV TEST TESTOVICH",
    phone="+998901234567",
    legal_info=(OneIdLegalInfo(le_tin="123456789", le_name="OOO TEST", is_basic=True),),
)


async def test_oneid_mock_roundtrip():
    adapter = get_oneid_adapter()
    url = adapter.authorize_url(state="abc", redirect_uri="http://x/cb", scope="test-scope")
    assert "state=abc" in url
    profile = await adapter.exchange_code(encode_mock_code(PROFILE))
    assert profile == PROFILE
    assert profile.legal_info[0].le_tin == "123456789"


async def test_oneid_snapshot_roundtrip():
    snapshot = PROFILE.to_snapshot()
    assert snapshot["pinfl"] == "12345678901234"
    assert snapshot["legal_info"][0]["le_name"] == "OOO TEST"
    assert OneIdProfile.from_snapshot(snapshot) == PROFILE


async def test_oneid_bad_code_maps_to_provider_error():
    adapter = get_oneid_adapter()
    with pytest.raises(OneIdError) as exc:
        await adapter.exchange_code("not-base64-json")
    assert exc.value.err_code == "ERR-INT-002"


async def test_eimzo_mock_roundtrip():
    identity = EimzoIdentity(
        challenge="ch-1", pinfl="12345678901234", full_name="TESTOV TEST", tin="123456789"
    )
    adapter = get_eimzo_adapter()
    parsed = await adapter.verify_signed_challenge(encode_mock_signed_challenge(identity))
    assert parsed == identity


async def test_eimzo_garbage_rejected():
    adapter = get_eimzo_adapter()
    with pytest.raises(EimzoError) as exc:
        await adapter.verify_signed_challenge("garbage")
    assert exc.value.err_code == "ERR-AUTH-004"


async def test_otp_sender_mock_records():
    sender = get_otp_sender()
    assert isinstance(sender, MockOtpSender)
    await sender.send(target_type="phone", target="+998901234567", code="123456")
    assert sender.sent[-1] == ("phone", "+998901234567", "123456")
    assert get_otp_sender() is sender  # singleton for introspection


async def test_otp_sender_uses_real_when_only_email_mode_is_real(monkeypatch):
    """sms_mode=mock + email_mode=real must not fall back to MockOtpSender: an
    explicitly-configured real channel must never be silently swallowed by the
    other, still-mocked switch (review finding, plan 03.5 Task 6). Mirrors
    test_sms_adapter.test_real_mode_returns_the_eskiz_sender's env-patch shape."""
    from app.config import get_settings
    from app.modules.integrations.adapters import otp_sender

    monkeypatch.setenv("SMS_MODE", "mock")
    monkeypatch.setenv("EMAIL_MODE", "real")
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_FROM", "Ruxsatnoma <robot@example.com>")
    get_settings.cache_clear()
    otp_sender.get_otp_sender.cache_clear()
    try:
        assert isinstance(otp_sender.get_otp_sender(), otp_sender.RealOtpSender)
    finally:
        get_settings.cache_clear()
        otp_sender.get_otp_sender.cache_clear()


async def test_oneid_profile_new_fields_roundtrip():
    profile = OneIdProfile(
        pinfl="12345678901234",
        full_name="T",
        auth_method="LEPKCSMETHOD",
        pkcs_legal_tin="123456789",
        valid=True,
        user_id="tester",
    )
    adapter = get_oneid_adapter()
    restored = await adapter.exchange_code(encode_mock_code(profile))
    assert restored == profile


async def test_mock_authorize_url_is_self_referential():
    """Before this, ONEID_MODE=mock's `authorize_url` returned the REAL
    sso.egov.uz URL — a browser (as opposed to a pytest client manufacturing
    `code` directly) that followed it landed on the state portal's error page
    with no route back (final review of stage 6.6, finding 1). It must instead
    point back at our own callback so the whole chain is walkable."""
    adapter = MockOneId()
    url = adapter.authorize_url(state="s", redirect_uri="http://testserver/cb", scope="ext")
    assert url.startswith("http://testserver/cb?")
    assert "sso.egov.uz" not in url
    assert "state=s" in url
    parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert parsed["state"] == ["s"]
    profile = await adapter.exchange_code(parsed["code"][0])
    assert profile == MOCK_DEMO_PROFILE
    # Obviously a demo, and not a shape any real citizen's PINFL could take.
    assert profile.pinfl == "99999999999999"
    assert "MOCK" in profile.full_name and "DEMO" in profile.full_name


def test_real_oneid_provider_url_shape_is_preserved():
    """`oneid_mode=real` still raises `NotImplementedError` (get_oneid_adapter,
    unaffected by this fix) — but the sso.egov.uz request shape the real
    adapter will need at stage 5.1 must survive the mock fix intact."""
    url = provider_authorize_url(state="s", redirect_uri="http://cb", scope="ext")
    assert url.startswith("https://sso.egov.uz/sso/oauth/Authorization.do?")
    assert "scope=ext" in url
    assert "state=s" in url
    assert "redirect_uri=http%3A%2F%2Fcb" in url


def test_oneid_real_mode_still_unimplemented(monkeypatch):
    """`get_oneid_adapter()`'s real-mode branch — untouched by this fix, and
    verified so: the mock-mode self-referential URL must never leak into it."""
    from app.config import get_settings

    monkeypatch.setenv("ONEID_MODE", "real")
    get_settings.cache_clear()
    try:
        with pytest.raises(NotImplementedError):
            get_oneid_adapter()
    finally:
        get_settings.cache_clear()


async def test_old_snapshot_without_new_fields_still_parses():
    old = {"pinfl": "12345678901234", "full_name": "T", "legal_info": []}
    profile = OneIdProfile.from_snapshot(old)
    assert profile.auth_method is None and profile.valid is None
