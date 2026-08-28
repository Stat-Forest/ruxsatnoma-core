"""OneID adapter seam (design/01: the mock lives inside auth until stage 3.4).

The real sso.egov.uz exchange (response_type=one_code -> one_authorization_code ->
one_access_token_identify) is stage 5.1. Field names mirror the live system's
response (pin, first/sur/mid name, mob_phone_no, legal_info[].le_tin/le_name/
is_basic) and MUST be re-verified against current OneID docs before the real
adapter is written.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

from app.config import get_settings
from app.modules.auth.adapters.mock_codec import decode_payload, encode_payload


@dataclass(frozen=True)
class OneIdLegalInfo:
    le_tin: str
    le_name: str
    is_basic: bool = False


@dataclass(frozen=True)
class OneIdProfile:
    pinfl: str
    full_name: str
    first_name: str | None = None
    sur_name: str | None = None
    mid_name: str | None = None
    birth_date: str | None = None
    phone: str | None = None
    passport: str | None = None
    legal_info: tuple[OneIdLegalInfo, ...] = field(default_factory=tuple)

    def to_snapshot(self) -> dict[str, Any]:
        data = asdict(self)
        data["legal_info"] = [asdict(li) for li in self.legal_info]
        return data

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> OneIdProfile:
        legal = tuple(OneIdLegalInfo(**li) for li in data.get("legal_info", []))
        return cls(**{**data, "legal_info": legal})


class OneIdError(Exception):
    """Provider unreachable or returned an error; err_code is ERR-INT-001/002."""

    def __init__(self, err_code: str) -> None:
        super().__init__(err_code)
        self.err_code = err_code


class OneIdAdapter(Protocol):
    def authorize_url(self, *, state: str, redirect_uri: str) -> str: ...

    async def exchange_code(self, code: str) -> OneIdProfile: ...


def encode_mock_code(profile: OneIdProfile) -> str:
    """Dev/test helper: a mock `code` IS the base64url profile (ruling 3)."""
    return encode_payload(profile.to_snapshot())


class MockOneId:
    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        query = urlencode(
            {
                "response_type": "one_code",
                "client_id": "mock",
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return f"https://sso.egov.uz/sso/oauth/Authorization.do?{query}"

    async def exchange_code(self, code: str) -> OneIdProfile:
        try:
            return OneIdProfile.from_snapshot(decode_payload(code))
        except (ValueError, TypeError) as exc:
            raise OneIdError("ERR-INT-002") from exc


def get_oneid_adapter() -> OneIdAdapter:
    if get_settings().oneid_mode == "mock":
        return MockOneId()
    raise NotImplementedError("real OneID adapter arrives at stage 5.1")
