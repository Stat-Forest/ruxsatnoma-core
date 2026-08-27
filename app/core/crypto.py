"""App-level encryption for sensitive columns (decision #27): Fernet keyed from secret_key.

Key rotation is out of scope for now: changing secret_key invalidates stored
ciphertexts (mfa_secret re-enrollment). Revisit before prod go-live.
"""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet

from app.config import get_settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = hashlib.sha256(get_settings().secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_str(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_str(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
