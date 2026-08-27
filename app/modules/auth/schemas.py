"""Pydantic schemas for auth API."""

import uuid
from typing import Any

from pydantic import BaseModel


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
