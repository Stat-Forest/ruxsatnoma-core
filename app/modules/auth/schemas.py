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
