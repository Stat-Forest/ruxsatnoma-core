"""Bound ERI certificates and document signatures (design/02 § signatures).
Level 2: this module reads `auth` through its service and never touches its tables."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

CERTIFICATE_STATUSES = ("active", "revoked", "expired")
VERIFICATION_STATUSES = ("valid", "invalid")


class Certificate(Base):
    """One row per (serial_number, issuer) pair — a certificate belongs to exactly
    one user, and a second user presenting it is refused, never given a second row."""

    __tablename__ = "certificates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    serial_number: Mapped[str]
    issuer: Mapped[str]
    subject: Mapped[str]
    pinfl_or_stir: Mapped[str]
    valid_from: Mapped[datetime]
    valid_to: Mapped[datetime]
    status: Mapped[str] = mapped_column(default="active")
    bound_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None]
    # Pre-flight ruling P3: `status` is the certificate's PKI state, per design/02.
    # "the user unbound it from their account" is our own concept and gets its own column.
    unbound_at: Mapped[datetime | None]

    __table_args__ = (
        # `f"{CERTIFICATE_STATUSES}"` renders as `('active', 'revoked',
        # 'expired')` -- Python's own tuple-of-str repr already IS valid SQL
        # `IN (...)` syntax, so the CHECK is derived from the one tuple
        # rather than retyping its members a second time (lesson: an
        # enum-ish column has ONE source of truth).
        CheckConstraint(f"status IN {CERTIFICATE_STATUSES}", name="status_valid"),
        CheckConstraint("valid_to > valid_from", name="validity_ordered"),
        UniqueConstraint("serial_number", "issuer", name="uq_certificate_identity"),
    )


class Signature(Base):
    """Evidence, not state: who signed which bytes with which key, and what the
    verification said AT THAT MOMENT (ruling 5). Rows are never updated."""

    __tablename__ = "signatures"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    object_type: Mapped[str]
    object_id: Mapped[uuid.UUID]
    purpose: Mapped[str]
    signer_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    certificate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("certificates.id"))
    doc_hash: Mapped[str]
    signature_value: Mapped[str]
    signed_at: Mapped[datetime]
    verification: Mapped[dict[str, Any]] = mapped_column(JSONB)
    verification_status: Mapped[str]

    __table_args__ = (
        CheckConstraint(
            f"verification_status IN {VERIFICATION_STATUSES}", name="verification_status_valid"
        ),
        Index("ix_signatures_object", "object_type", "object_id"),
        Index(
            "uq_signatures_valid_purpose",
            "object_type",
            "object_id",
            "purpose",
            unique=True,
            postgresql_where=text("verification_status = 'valid'"),
        ),
    )
