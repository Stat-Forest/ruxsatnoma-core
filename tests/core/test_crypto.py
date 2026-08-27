"""Fernet-based app-level encryption for sensitive columns (decision #27)."""

import pytest
from cryptography.fernet import InvalidToken

from app.core.crypto import decrypt_str, encrypt_str


def test_roundtrip():
    token = encrypt_str("JBSWY3DPEHPK3PXP")
    assert token != "JBSWY3DPEHPK3PXP"
    assert decrypt_str(token) == "JBSWY3DPEHPK3PXP"


def test_tokens_differ_per_call():
    assert encrypt_str("x") != encrypt_str("x")  # Fernet embeds IV/time


def test_tampered_token_rejected():
    token = encrypt_str("x")
    with pytest.raises(InvalidToken):
        decrypt_str(token[:-4] + "AAAA")
