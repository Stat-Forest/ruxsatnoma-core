"""Auth module models (design/02 § auth). Staff log in with password+MFA;
applicants/prosecutors have no password (login/password_hash null, decision #32).

Territory FKs closed in stage 3.3a (migration 0004). user_delegations deferred
entirely (ruling 10); applicants, representations, user_consents are stage 3.2b.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IPAddressString, uuid7


class Role(Base):
    """System role catalog; 11 seeded rows (migration 0003). Approval limits — decision #29.

    Roles are archived, never deleted — status column (ruling 11, 3.3b)."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(unique=True)
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    description: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    is_system: Mapped[bool] = mapped_column(default=False)
    max_approve_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    max_approve_area: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    status: Mapped[str] = mapped_column(default="active")

    __table_args__ = (CheckConstraint("status IN ('active', 'archived')", name="status_valid"),)


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
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id"))
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))
    region_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("regions.id"))
    district_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("districts.id"))
    phone: Mapped[str | None]
    phone_verified_at: Mapped[datetime | None]
    email: Mapped[str | None] = mapped_column(CITEXT)
    email_verified_at: Mapped[datetime | None]
    # Notification language (plan 03.5 ruling 17); PATCH /auth/me is the OTP-guarded
    # contact route, so the language switch gets its own PUT /auth/me/language.
    language: Mapped[str] = mapped_column(server_default="uz_cyrl", default="uz_cyrl")
    mfa_secret: Mapped[str | None]  # Fernet-encrypted TOTP secret (ruling 5)
    oneid_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # 3.2b ruling 4
    status: Mapped[str] = mapped_column(default="active")
    must_change_password: Mapped[bool] = mapped_column(default=False)  # ruling 8
    valid_until: Mapped[date | None]
    failed_login_count: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active', 'blocked', 'deleted')", name="status_valid"),
        CheckConstraint(r"pinfl IS NULL OR pinfl ~ '^[0-9]{14}$'", name="pinfl_format"),
        CheckConstraint(
            "language IN ('uz_cyrl', 'uz_latn', 'ru', 'kaa', 'en')", name="language_valid"
        ),
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
    """One-time codes: MFA handoff tokens (ruling 6); phone/email verify, their
    verified-tokens, and E-IMZO challenges live here too (3.2b)."""

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
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('phone_verify', 'email_verify', 'mfa', 'password_reset',"
            " 'phone_verify_token', 'email_verify_token', 'eimzo_challenge')",
            name="purpose_valid",
        ),
        Index("ix_otp_codes_code_hash", "code_hash"),
    )


class Applicant(Base):
    """The person/org applications are filed for (design/02 § applicants).

    individual ⇔ pinfl, legal ⇔ stir (identity_by_kind); the full unique on each
    column subsumes design/02's partial unique — the CHECK already restricts the
    column to one kind. owner_user_id: individual's 1:1 account; legal has none
    (representatives act, decision #9)."""

    __tablename__ = "applicants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    kind: Mapped[str]
    pinfl: Mapped[str | None] = mapped_column(unique=True)
    stir: Mapped[str | None] = mapped_column(unique=True)
    name: Mapped[str]
    region_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("regions.id"))
    district_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("districts.id"))
    address: Mapped[str | None]
    phone: Mapped[str | None]
    email: Mapped[str | None] = mapped_column(CITEXT)
    requisites: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), unique=True)
    verified_at: Mapped[datetime | None]
    verify_source: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('individual', 'legal')", name="kind_valid"),
        CheckConstraint(
            "(kind = 'individual' AND pinfl IS NOT NULL AND stir IS NULL)"
            " OR (kind = 'legal' AND stir IS NOT NULL AND pinfl IS NULL)",
            name="identity_by_kind",
        ),
        CheckConstraint("pinfl IS NULL OR pinfl ~ '^[0-9]{14}$'", name="pinfl_format"),
        CheckConstraint("stir IS NULL OR stir ~ '^[0-9]{9}$'", name="stir_format"),
    )


class Representation(Base):
    """Who may act for a legal applicant and on what basis (decision #9).

    poa_file_id FK to media_files closed in 3.3b (the deferred-FK pattern from
    3.2b is resolved). Effectiveness is checked on read: status='active' AND not
    past valid_until (ruling 14); the expiry job is 3.4+."""

    __tablename__ = "representations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    applicant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applicants.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    basis: Mapped[str]
    poa_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"))
    valid_from: Mapped[date]
    valid_until: Mapped[date | None]
    status: Mapped[str] = mapped_column(default="active")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("basis IN ('director_registry', 'poa', 'org_eri')", name="basis_valid"),
        CheckConstraint("status IN ('active', 'expired', 'revoked')", name="status_valid"),
        CheckConstraint(
            "basis <> 'poa' OR (poa_file_id IS NOT NULL AND valid_until IS NOT NULL)",
            name="poa_requires_file_and_term",
        ),
        Index(
            "uq_representations_active",
            "applicant_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_representations_user", "user_id"),
    )


class UserConsent(Base):
    """С2 consents; without them registration does not proceed (design/02)."""

    __tablename__ = "user_consents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    doc_type: Mapped[str]
    doc_version: Mapped[str]
    accepted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    ip: Mapped[str | None] = mapped_column(IPAddressString)

    __table_args__ = (
        CheckConstraint("doc_type IN ('privacy_policy', 'offer')", name="doc_type_valid"),
        UniqueConstraint("user_id", "doc_type", "doc_version", name="uq_user_consents_doc"),
    )
