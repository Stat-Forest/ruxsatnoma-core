"""Pydantic schemas for auth API."""

import re
import uuid
from typing import Any, Literal

from email_validator import validate_email
from pydantic import BaseModel, model_validator


class UserOut(BaseModel):
    id: uuid.UUID
    full_name: str
    login: str | None
    phone: str | None
    email: str | None
    must_change_password: bool


class RoleOut(BaseModel):
    code: str
    name: dict[str, Any]


class ZoneOut(BaseModel):
    region_id: uuid.UUID | None
    district_id: uuid.UUID | None
    organization_id: uuid.UUID | None


class MeOut(BaseModel):
    user: UserOut
    role: RoleOut
    permissions: list[str]
    zone: ZoneOut
    # Cross-origin adminka (ruling 3): Set-Cookie is never readable by JS, and a
    # host-only cookie isn't readable cross-origin either way, so the double-submit
    # value is also returned in the body — the frontend holds it in memory and
    # replays it as X-CSRF-Token. Safe from a GET: a cross-origin attacker can't
    # read this response because their origin isn't in the CORS allowlist.
    csrf_token: str
    # True for the sys_admin superuser role (ruling 2): require_permission lets it
    # through without consulting codes, so `permissions` below is the whole registry
    # for this user, not its (possibly empty) personal grants — the frontend needs
    # this flag to tell "holds every code today" apart from "is the superuser, and
    # would still pass a gate for a code added tomorrow".
    is_superuser: bool


class OneIdAuthorizeOut(BaseModel):
    redirect_url: str


class EimzoChallengeOut(BaseModel):
    challenge: str


class EimzoLoginIn(BaseModel):
    signed_challenge: str


class LoginIn(BaseModel):
    login: str
    password: str


class LoginOut(BaseModel):
    mfa_required: bool
    mfa_token: str


class MfaIn(BaseModel):
    mfa_token: str
    code: str


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str


def _validate_target_format(target: str, purpose: str) -> None:
    """Raise ValueError unless `target` matches the format `purpose` implies.

    Shared by OtpRequestIn and OtpVerifyIn so both anonymous OTP endpoints
    reject malformed targets at the schema level, before they ever reach the
    service/audit layer.
    """
    if purpose == "phone_verify":
        if not re.fullmatch(r"\+998[0-9]{9}", target):
            raise ValueError("phone must be +998XXXXXXXXX")
    else:
        # No DNS lookups in request validation (tests run offline).
        validate_email(target, check_deliverability=False)


class OtpRequestIn(BaseModel):
    target_type: Literal["phone", "email"]
    target: str
    purpose: Literal["phone_verify", "email_verify"]

    @model_validator(mode="after")
    def _validate_target(self) -> OtpRequestIn:
        pairs = {"phone": "phone_verify", "email": "email_verify"}
        if pairs[self.target_type] != self.purpose:
            raise ValueError("purpose does not match target_type")
        _validate_target_format(self.target, self.purpose)
        return self


class OtpVerifyIn(BaseModel):
    target: str
    code: str
    purpose: Literal["phone_verify", "email_verify"]

    @model_validator(mode="after")
    def _validate_target(self) -> OtpVerifyIn:
        _validate_target_format(self.target, self.purpose)
        return self


class OtpVerifyOut(BaseModel):
    otp_token: str
