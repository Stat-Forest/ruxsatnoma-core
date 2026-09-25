"""Pydantic schemas for auth API."""

import re
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from email_validator import validate_email
from pydantic import BaseModel, EmailStr, Field, StringConstraints, model_validator

from app.core.schemas import BlobStr, CodeStr, NameStr, PasswordStr


class UserOut(BaseModel):
    id: uuid.UUID
    full_name: str
    login: str | None
    phone: str | None
    email: str | None
    must_change_password: bool
    language: str
    # The caller's own PINFL (nullable: `users.pinfl` is). Read by the
    # adminka's mock E-IMZO signer (`lib/eimzoMock.ts`) so a staff signature
    # carries the signed-in user's identity instead of a hand-typed one.
    pinfl: str | None


class RoleOut(BaseModel):
    code: str
    name: dict[str, Any]


class ZoneOut(BaseModel):
    region_id: uuid.UUID | None
    district_id: uuid.UUID | None
    organization_id: uuid.UUID | None


class ConsentsIn(BaseModel):
    # Version codes (`"1.0"`), never the document text itself — compared
    # against `settings_store`'s own `privacy_policy_version`/`offer_version`
    # (`service.py::_check_consents_current`).
    privacy_policy: CodeStr
    offer: CodeStr


class CompleteRegistrationIn(BaseModel):
    consents: ConsentsIn
    # The anchored pattern alone bounds this (M6, final review):
    # `test_request_bounds.py`'s walker now ignores an ESCAPED `\+`/`\*` (a
    # literal character, not an open quantifier), so the `max_length=13`
    # this field used to carry alongside the pattern — a workaround for that
    # walker limitation — is gone; the pattern was always the real bound.
    phone: str = Field(pattern=r"^\+998[0-9]{9}$")
    # An OTP handoff token (`service.new_token()`, `secrets.token_urlsafe(32)`)
    # — never typed by hand, but the same "human-typed token" class `PasswordStr`
    # already covers (never stripped: it is compared byte for byte).
    otp_token: PasswordStr
    email: EmailStr | None = None
    region_id: uuid.UUID | None = None
    district_id: uuid.UUID | None = None
    # Same bound as `ApplicantAddressIn.address` above, for the same reason:
    # this route no longer collects it in practice (ruling #113 moved the
    # requisite to `ApplicationWizardPage`'s own submit), but the field stays
    # reachable and a whitespace-only address must not pass as a real one.
    address: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
        | None
    ) = None


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


class ApplicantAddressIn(BaseModel):
    """`PATCH /auth/applicants/{applicant_id}/address` (ruling #113): the one
    field the route exists for. `StringConstraints(strip_whitespace=True,
    ...)`, not a plain `Field(min_length=1)` — a whitespace-only address has a
    nonzero length and would otherwise pass as if it named a real place
    (`permits/schemas.py::DuplicateIn.reason` is the same idiom for the same
    reason). `max_length` is this codebase's own free-text convention
    (`permits/schemas.py::DuplicateIn.reason`,
    `norms/schemas.py::TariffIn.basis`), not a limit named anywhere in
    `tz/13`'s form 1-ilova."""

    address: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


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
    # E-IMZO's own signed envelope, posted straight through to the adapter
    # (`RealEimzo.verify_signed_challenge`) — the same PKCS#7-shaped blob
    # `signatures.sign()` verifies, so the same bound.
    signed_challenge: BlobStr


class LoginIn(BaseModel):
    login: CodeStr
    password: PasswordStr


class LoginOut(BaseModel):
    """Answer to the password step, in one of two shapes.

    MFA on: `mfa_required` true, `mfa_token` set, `me` null — the client must
    still call /auth/mfa/verify. MFA off (`mfa_enabled`): `mfa_required` false,
    `mfa_token` null, `me` set — the session cookies are already on THIS
    response and there is no second step to take.
    """

    mfa_required: bool
    mfa_token: str | None = None
    me: MeOut | None = None


class MfaIn(BaseModel):
    mfa_token: PasswordStr
    code: PasswordStr


class PasswordChangeIn(BaseModel):
    old_password: PasswordStr
    new_password: PasswordStr


# `login: CodeStr` on all three below (C2, final review) matches `LoginIn.login`
# exactly — stripped, 64 — rather than the wider, unstripped 150 they used to
# carry: three different bounds for the same field is what let a login with
# trailing whitespace slip past the reset flow's own uniqueness lookup while
# `LoginIn` already refused it. The adminka sets no client-side maxLength on
# this input at all (`LoginPage.tsx`), so nothing pins it above 64.


class PasswordForgotLookupIn(BaseModel):
    login: CodeStr


class PasswordForgotLookupOut(BaseModel):
    """Masked contacts a self-service reset can go to; `None` = not filled in.

    An unknown login answers `(None, None)` too — indistinguishable from a
    staff member whose card has neither contact (decision #208).
    """

    phone: str | None
    email: str | None


class PasswordForgotSendIn(BaseModel):
    login: CodeStr
    channel: Literal["phone", "email"]


class PasswordForgotResetIn(BaseModel):
    login: CodeStr
    channel: Literal["phone", "email"]
    # `PasswordStr` (I4, final review): a human-typed OTP code, the same type
    # `MfaIn.code`/`OtpVerifyIn.code` already use for the identical reason —
    # this one was the odd one out, a bare `Field(min_length=1, max_length=16)`
    # that the new stripped-required-text check would otherwise flag.
    code: PasswordStr
    new_password: PasswordStr


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
    # A phone number OR an email address, human-typed — unlike `LoginIn.
    # login` (a dedicated username, never an email: `repo.get_user_by_login`
    # matches only `users.login`), this field genuinely carries an email
    # half the time, and RFC 5321 allows one up to 254 characters. `CodeStr`
    # (64) refused a real, syntactically valid long address here before fix
    # round 1 (`test_a_long_but_valid_email_target_is_not_refused_on_
    # length`) — `NameStr`'s 255-char bound is reused for its LENGTH only,
    # not its "name" semantics. `_validate_target_format` below still does
    # the real format check; this is only the upper bound
    # `test_request_bounds.py` requires.
    target: NameStr
    purpose: Literal["phone_verify", "email_verify"]

    @model_validator(mode="after")
    def _validate_target(self) -> OtpRequestIn:
        pairs = {"phone": "phone_verify", "email": "email_verify"}
        if pairs[self.target_type] != self.purpose:
            raise ValueError("purpose does not match target_type")
        _validate_target_format(self.target, self.purpose)
        return self


class OtpVerifyIn(BaseModel):
    # Same reasoning as `OtpRequestIn.target` above: this can be an email too.
    target: NameStr
    code: PasswordStr
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
    signed_challenge: BlobStr | None = None
    poa_file_id: uuid.UUID | None = None
    valid_until: date | None = None
    name: NameStr | None = None

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
    signed_challenge: BlobStr | None = None
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
    # The anchored pattern alone bounds this (M6, final review) — see
    # `CompleteRegistrationIn.phone`'s own comment.
    phone: str | None = Field(default=None, pattern=r"^\+998[0-9]{9}$")
    email: EmailStr | None = None
    # Optional since decision #150: a staff member changes their own phone number
    # with no SMS code, because no SMS is ever sent to them and a confirmation they
    # cannot receive is a number they cannot change. Whether the token is REQUIRED
    # is a question about the caller's role, so `service.update_contact` decides it
    # — this schema cannot see the user.
    otp_token: PasswordStr | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ContactUpdateIn:
        if (self.phone is None) == (self.email is None):
            raise ValueError("exactly one of phone/email must be set")
        return self


class LanguageIn(BaseModel):
    language: Literal["uz_cyrl", "uz_latn", "ru", "kaa", "en"]
