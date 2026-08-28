"""E-IMZO adapter seam. Real PKCS7/CRL verification is stage 3.8/5.2; the mock
treats a base64url JSON identity as a valid signature (ruling 3). A legal-entity
certificate carries the org STIR in `tin` alongside the signer's pinfl."""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol

from app.config import get_settings
from app.modules.auth.adapters.mock_codec import decode_payload, encode_payload


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
    def __init__(self, err_code: str = "ERR-AUTH-004") -> None:
        super().__init__(err_code)
        self.err_code = err_code


class EimzoAdapter(Protocol):
    async def verify_signed_challenge(self, signed_challenge: str) -> EimzoIdentity: ...


def encode_mock_signed_challenge(identity: EimzoIdentity) -> str:
    return encode_payload(identity.to_payload())


class MockEimzo:
    async def verify_signed_challenge(self, signed_challenge: str) -> EimzoIdentity:
        try:
            return EimzoIdentity.from_payload(decode_payload(signed_challenge))
        except (ValueError, TypeError) as exc:
            raise EimzoError() from exc


def get_eimzo_adapter() -> EimzoAdapter:
    if get_settings().eimzo_mode == "mock":
        return MockEimzo()
    raise NotImplementedError("real E-IMZO adapter arrives at stage 3.8/5.2")
