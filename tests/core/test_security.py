"""Password hashing/policy, opaque tokens, TOTP helpers."""

import pytest

from app.core.errors import DomainError
from app.core.security import (
    hash_password,
    hash_token,
    new_token,
    new_totp_secret,
    totp_provisioning_uri,
    validate_password_policy,
    verify_password,
    verify_totp,
)


def test_password_hash_roundtrip():
    h = hash_password("Str0ng!pass")
    assert h.startswith("$argon2id$")
    assert verify_password("Str0ng!pass", h)
    assert not verify_password("wrong", h)


@pytest.mark.parametrize(
    "bad",
    ["Sh1!", "nouppercase1!", "NOLOWERCASE1!", "NoDigits!!", "NoSpecial11A"],
)
def test_password_policy_rejects(bad):
    with pytest.raises(DomainError):
        validate_password_policy(bad)


def test_password_policy_accepts():
    validate_password_policy("Str0ng!pass")  # no raise


def test_token_and_hash():
    t1, t2 = new_token(), new_token()
    assert t1 != t2 and len(t1) >= 43
    assert hash_token(t1) != hash_token(t2)
    assert len(hash_token(t1)) == 64  # sha256 hex


def test_totp_roundtrip():
    import pyotp

    secret = new_totp_secret()
    code = pyotp.TOTP(secret).now()
    assert verify_totp(secret, code)
    assert not verify_totp(secret, "000000")
    uri = totp_provisioning_uri(secret, "admin")
    assert uri.startswith("otpauth://totp/") and "Ruxsatnoma" in uri
