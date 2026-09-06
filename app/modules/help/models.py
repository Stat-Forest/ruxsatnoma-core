"""help — subsystem 12: FAQ and support tickets, the lowest-priority module in
the whole project plan (`tz/02`: "[низкий приоритет]"). Kept deliberately flat
(plan `04.6-4.8-public-help.md` ruling R6): FAQ is plain CRUD over a status,
never versioned; tickets carry one linear status machine, never an SLA clock.

Ticket message bodies are authored by whoever opened the ticket (any
authenticated user, staff or applicant) — never anonymous, unlike `public`'s
citizen_appeals, but still never interpolated into a raised exception or a log
line (ruling R5's discipline applies here too, for the same reason)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

TICKET_NUMBER_PREFIX = "ST"
FAQ_STATUSES = ("draft", "published", "archived")
TICKET_STATUSES = ("new", "in_progress", "resolved", "closed")
TICKET_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "new": ("in_progress", "closed"),
    "in_progress": ("resolved", "closed"),
    "resolved": ("closed",),
    "closed": (),
}


class FaqItem(Base):
    """A single FAQ entry. `question`/`answer` are `LocalizedName`-shaped JSONB
    (`app/core/schemas.py`), matching every other multilingual field in this
    codebase — no versioning, no supersede-by-archive: this is help copy, not
    a legal or priced document, so an edit in place is fine (ruling R6)."""

    __tablename__ = "faq_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    category: Mapped[str | None]
    question: Mapped[dict[str, Any]] = mapped_column(JSONB)
    answer: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sort_order: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="draft")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(f"status IN {FAQ_STATUSES}", name="status_valid"),
        Index("ix_faq_items_status_sort", "status", "sort_order"),
    )


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    number: Mapped[str] = mapped_column(unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    subject: Mapped[str]
    status: Mapped[str] = mapped_column(default="new")
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    closed_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(f"status IN {TICKET_STATUSES}", name="status_valid"),
        CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL)", name="closed_at_consistent"
        ),
    )


class SupportTicketMessage(Base):
    """Append-only in practice (nothing here ever updates or deletes a
    message) but not enforced by a DB trigger — unlike `audit_log`, a
    support-ticket message carries no legal weight, so the append-only
    guarantee is not worth the same migration cost here."""

    __tablename__ = "support_ticket_messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("support_tickets.id"), index=True)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str]
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
