"""Auth module models (design/02 § auth). Staff log in with password+MFA;
applicants/prosecutors have no password (login/password_hash null, decision #32).

Deferred FKs (ruling 4): organization_id/region_id/district_id become FKs in
stage 3.3 admin. user_delegations deferred entirely (ruling 10); applicants,
representations, user_consents are stage 3.2b.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, func
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IPAddressString, uuid7


class Role(Base):
    """System role catalog; 11 seeded rows (migration 0003). Approval limits — decision #29."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(unique=True)
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    description: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    is_system: Mapped[bool] = mapped_column(default=False)
    max_approve_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    max_approve_area: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))


class User(Base):
    """All accounts: staff, applicants, prosecutors. Never physically deleted
    (status='deleted'); audit_log.user_id FK is NO ACTION (ruling 3)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    login: Mapped[str | None] = mapped_column(CITEXT, unique=True)
    password_hash: Mapped[str | None]
    pinfl: Mapped[str | None] = mapped_column(unique=True)
    full_name: Mapped[str]
    position: Mapped[str | None]
    organization_id: Mapped[uuid.UUID | None]  # FK in 3.3 (ruling 4)
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))
    region_id: Mapped[uuid.UUID | None]  # FK in 3.3
    district_id: Mapped[uuid.UUID | None]  # FK in 3.3
    phone: Mapped[str | None]
    phone_verified_at: Mapped[datetime | None]
    email: Mapped[str | None] = mapped_column(CITEXT)
    email_verified_at: Mapped[datetime | None]
    mfa_secret: Mapped[str | None]  # Fernet-encrypted TOTP secret (ruling 5)
    status: Mapped[str] = mapped_column(default="active")
    must_change_password: Mapped[bool] = mapped_column(default=False)  # ruling 8
    valid_until: Mapped[date | None]
    failed_login_count: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active', 'blocked', 'deleted')", name="status_valid"),
        CheckConstraint(r"pinfl IS NULL OR pinfl ~ '^\d{14}$'", name="pinfl_format"),
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"), primary_key=True)
    permission_code: Mapped[str] = mapped_column(primary_key=True)


class UserPermission(Base):
    """Per-user grants beyond the role (C23)."""

    __tablename__ = "user_permissions"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    permission_code: Mapped[str] = mapped_column(primary_key=True)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    granted_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Session(Base):
    """Server-side sessions (decision #30): opaque token's sha256 only."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    token_hash: Mapped[str] = mapped_column(unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    csrf_token: Mapped[str]  # double-submit value (ruling 7); not secret at rest
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    ip: Mapped[str | None] = mapped_column(IPAddressString)
    user_agent: Mapped[str | None]
    revoked_at: Mapped[datetime | None]

    __table_args__ = (Index("ix_sessions_user", "user_id"),)


class OtpCode(Base):
    """One-time codes: MFA handoff tokens now (ruling 6); phone/email verify in 3.2b."""

    __tablename__ = "otp_codes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    target_type: Mapped[str | None]  # phone / email; null for purpose='mfa'
    target: Mapped[str | None]
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    code_hash: Mapped[str]
    purpose: Mapped[str]
    expires_at: Mapped[datetime]
    attempts: Mapped[int] = mapped_column(default=0)
    used_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('phone_verify', 'email_verify', 'mfa', 'password_reset')",
            name="purpose_valid",
        ),
        Index("ix_otp_codes_code_hash", "code_hash"),
    )
