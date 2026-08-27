"""Passwords (Argon2id), opaque session/MFA tokens, TOTP (design/01: core/security)."""

import hashlib
import re
import secrets

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.errors import err

_hasher = PasswordHasher()  # argon2id, library defaults

_POLICY_CHECKS: list[tuple[str, str]] = [
    (r".{8,}", "at least 8 characters"),
    (r"[A-Z]", "an uppercase letter"),
    (r"[a-z]", "a lowercase letter"),
    (r"\d", "a digit"),
    (r"[^A-Za-z0-9]", "a special character"),
]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError, VerificationError, InvalidHashError:
        return False


def validate_password_policy(password: str) -> None:
    """tz/11: >=8 chars, upper + lower + digit + special. Raises ERR-VAL-001."""
    missing = [need for pattern, need in _POLICY_CHECKS if not re.search(pattern, password)]
    if missing:
        raise err("ERR-VAL-001", details={"password_policy": missing})


def new_token() -> str:
    """Opaque token for sessions and MFA handoff; only its sha256 is stored."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def totp_provisioning_uri(secret: str, login: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=login, issuer_name="Ruxsatnoma")
