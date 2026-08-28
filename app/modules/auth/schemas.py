"""Pydantic schemas for auth API."""

import re
import uuid
from datetime import date, datetime
from typing import Any, Literal

from email_validator import validate_email
from pydantic import BaseModel, EmailStr, Field, model_validator


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


class ConsentsIn(BaseModel):
    privacy_policy: str
    offer: str


class CompleteRegistrationIn(BaseModel):
    consents: ConsentsIn
    phone: str = Field(pattern=r"^\+998[0-9]{9}$")
    otp_token: str
    email: EmailStr | None = None
    region_id: uuid.UUID | None = None
    district_id: uuid.UUID | None = None
    address: str | None = None


class ApplicantOut(BaseModel):
    id: uuid.UUID
    kind: str
    pinfl: str | None
    stir: str | None
    name: str
    phone: str | None
    email: str | None
    region_id: uuid.UUID | None
    district_id: uuid.UUID | None
    address: str | None
    verified_at: datetime | None


class RepresentationOut(BaseModel):
    id: uuid.UUID
    applicant: ApplicantOut
    basis: str
    valid_from: date
    valid_until: date | None
    status: str


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
    # Applicant-role only (ruling 16); every other role keeps the defaults below so
    # existing staff suites stay green without touching their assertions.
    applicant: ApplicantOut | None = None
    representations: list[RepresentationOut] = []
    registration_complete: bool = True


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


class AttachLegalIn(BaseModel):
    stir: str = Field(pattern=r"^[0-9]{9}$")
    basis: Literal["org_eri", "director_registry", "poa"]
    signed_challenge: str | None = None
    poa_file_id: uuid.UUID | None = None
    valid_until: date | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _validate_basis_fields(self) -> AttachLegalIn:
        if self.basis == "org_eri" and not self.signed_challenge:
            raise ValueError("org_eri requires signed_challenge")
        if self.basis == "poa" and not (self.poa_file_id and self.valid_until and self.name):
            raise ValueError("poa requires poa_file_id, valid_until and name")
        return self


class AttachLegalOut(BaseModel):
    applicant: ApplicantOut
    representation: RepresentationOut


class AddRepresentationIn(BaseModel):
    user_pinfl: str = Field(pattern=r"^[0-9]{14}$")
    basis: Literal["org_eri", "director_registry", "poa"]
    signed_challenge: str | None = None
    poa_file_id: uuid.UUID | None = None
    valid_until: date | None = None

    @model_validator(mode="after")
    def _validate_basis_fields(self) -> AddRepresentationIn:
        if self.basis == "org_eri" and not self.signed_challenge:
            raise ValueError("org_eri requires signed_challenge")
        if self.basis == "poa" and not (self.poa_file_id and self.valid_until):
            raise ValueError("poa requires poa_file_id and valid_until")
        return self


class ContactUpdateIn(BaseModel):
    phone: str | None = Field(default=None, pattern=r"^\+998[0-9]{9}$")
    email: EmailStr | None = None
    otp_token: str

    @model_validator(mode="after")
    def _exactly_one(self) -> ContactUpdateIn:
        if (self.phone is None) == (self.email is None):
            raise ValueError("exactly one of phone/email must be set")
        return self
