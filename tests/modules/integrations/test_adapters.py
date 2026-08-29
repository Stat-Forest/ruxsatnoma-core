"""Adapter seam: mock codecs, factories, prod guard lives in test_config."""

import pytest

from app.modules.integrations.adapters.eimzo import (
    EimzoError,
    EimzoIdentity,
    encode_mock_signed_challenge,
    get_eimzo_adapter,
)
from app.modules.integrations.adapters.oneid import (
    MockOneId,
    OneIdError,
    OneIdLegalInfo,
    OneIdProfile,
    encode_mock_code,
    get_oneid_adapter,
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


def test_authorize_url_carries_scope():
    url = MockOneId().authorize_url(state="s", redirect_uri="http://cb", scope="ext")
    assert "scope=ext" in url


async def test_old_snapshot_without_new_fields_still_parses():
    old = {"pinfl": "12345678901234", "full_name": "T", "legal_info": []}
    profile = OneIdProfile.from_snapshot(old)
    assert profile.auth_method is None and profile.valid is None
