"""Pydantic schemas for the user-administration API (С23): CRUD, credentials
handout, block/unblock/delete. Split out of `users_service.py` per the stage plan
once the module would otherwise grow past ~400 lines.
"""

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, Field

# Matches the DB's `pinfl_format` CHECK (ASCII-only digits, migration 0007):
# catching the shape here means a bad value 422s at the schema boundary instead of
# surfacing as an IntegrityError (500) — same reasoning as admin.schemas.Stir.
Pinfl = Annotated[str, Field(pattern=r"^[0-9]{14}$")]


class UserFilters(BaseModel):
    """`GET /admin/users` query filters; every field is optional (no filter)."""

    role_code: str | None = None
    status: str | None = None
    organization_id: uuid.UUID | None = None
    region_id: uuid.UUID | None = None
    q: str | None = None  # ILIKE against login/full_name/pinfl


class UserAdminOut(BaseModel):
    id: uuid.UUID
    login: str | None
    full_name: str
    pinfl: str | None
    position: str | None
    role_id: uuid.UUID
    role_code: str
    organization_id: uuid.UUID | None
    region_id: uuid.UUID | None
    district_id: uuid.UUID | None
    phone: str | None
    email: str | None
    status: str
    must_change_password: bool
    valid_until: date | None
    last_login_at: datetime | None
    created_at: datetime


class UserCreateIn(BaseModel):
    """Staff roles only (`role_code != "applicant"`, ruling 8/9) — applicants are
    born via OneID/E-IMZO, never created by hand here, so `login` is mandatory."""

    login: str
    full_name: str
    role_code: str
    pinfl: Pinfl | None = None
    position: str | None = None
    organization_id: uuid.UUID | None = None
    region_id: uuid.UUID | None = None
    district_id: uuid.UUID | None = None
    phone: str | None = None
    email: str | None = None
    valid_until: date | None = None


class UserPatchIn(BaseModel):
    """All fields optional — only keys present in the request body are touched
    (`exclude_unset=True` in the service), same convention as `OrganizationPatch`.
    Assigning a non-applicant `role_code` requires a `login` (stored or in the same
    patch) — ruling 9."""

    full_name: str | None = None
    position: str | None = None
    pinfl: Pinfl | None = None
    phone: str | None = None
    email: str | None = None
    organization_id: uuid.UUID | None = None
    region_id: uuid.UUID | None = None
    district_id: uuid.UUID | None = None
    role_code: str | None = None
    login: str | None = None
    valid_until: date | None = None


class UserBlockIn(BaseModel):
    reason: str


class UserCreatedOut(BaseModel):
    """201 body of `POST /admin/users` — the one and only time the plaintext
    one-time password is ever returned (ruling 8)."""

    user: UserAdminOut
    one_time_password: str
    totp_uri: str


class OneTimePasswordOut(BaseModel):
    one_time_password: str


class TotpUriOut(BaseModel):
    totp_uri: str
