"""base64url-JSON codec shared by the mock adapters (ruling 3)."""

import base64
import binascii
import json
from typing import Any


def encode_payload(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()


def decode_payload(raw: str) -> dict[str, Any]:
    """Raises ValueError on anything that is not base64url-wrapped JSON object."""
    try:
        decoded = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("not a mock payload") from exc
    if not isinstance(decoded, dict):
        raise ValueError("not a mock payload")
    return decoded
