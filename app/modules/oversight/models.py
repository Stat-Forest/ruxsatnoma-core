"""Oversight module models (design/02 § oversight; plan `04.2-4.4-oversight-
dashboard`). Level 5 reader: this module owns exactly these two tables and
reads every other module's tables read-only (design/01 rule 5) — it never
imports another module's `models.py` for a WRITE, only for a `select()`.

`risk_indicators` accumulates RI-01..15 (`tz/10`). Most codes are already
being tagged today, at the moment they happen, as `audit_log.extra =
{"risk_indicator": "RI-xx"}` rows by the modules that can see the event
(`payments.service`, `permits.service`, `applications.service`/`.jobs`,
`signatures.service`, `norms.service` — see `service.py`'s `harvest()` for the
full catalogue). This module's job for those is a HARVESTER, not a detector:
`idempotency_key` is set to the harvested `audit_log` row's own `id`, so one
source row can never produce two `risk_indicators` rows. RI-03 (overlapping
active permits on one contour) has no such source anywhere in the codebase —
this module raises it directly, from its own periodic sweep over `permits`,
keyed on a deterministic `uuid5` of the offending pair since there is no audit
row to key off.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

# tz/10's full catalogue. Only a subset (see service.py) has a working
# detector today; the CHECK still admits all 15 so a future detector never
# needs a migration to store its own code (lesson: an enum-ish column has one
# source of truth — this tuple, mirrored by hand in schemas.py's Literal and
# pinned equal by test_models.py).
RISK_INDICATOR_CODES = tuple(f"RI-{n:02d}" for n in range(1, 16))

RISK_INDICATOR_LEVELS = ("low", "medium", "high", "critical")

# tz/10's own severity per code — the ONE place that mapping is spelled out;
# every writer (harvest or direct raise) reads it rather than re-deciding
# severity per call site, which is exactly the drift class the "enum-ish
# column" lesson warns about.
RI_LEVEL_BY_CODE: dict[str, str] = {
    "RI-01": "high",
    "RI-02": "high",
    "RI-03": "high",
    "RI-04": "high",
    "RI-05": "high",
    "RI-06": "critical",
    "RI-07": "medium",
    "RI-08": "high",
    "RI-09": "medium",
    "RI-10": "critical",
    "RI-11": "high",
    "RI-12": "high",
    "RI-13": "high",
    "RI-14": "low",
    "RI-15": "medium",
}
assert set(RI_LEVEL_BY_CODE) == set(RISK_INDICATOR_CODES)

RISK_INDICATOR_STATUSES = ("new", "in_review", "closed")

# Shared by both tables (design/02): "internal" while RN is not connected
# (tz/09) — nothing here ever moves past it until a real RN contract exists.
RN_STATUSES = ("internal", "pending", "sent", "failed")


class OversightEvent(Base):
    """The stream of legally significant events (design/02 § oversight),
    accumulating from day one. Written by `service.record_event`, subscribed
    to the five bus events `applications`/`payments` already publish
    (`app/event_subscriptions.py`) — no other module is modified to produce
    these."""

    __tablename__ = "oversight_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    event_type: Mapped[str]
    object_type: Mapped[str | None]
    object_id: Mapped[uuid.UUID | None]
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    correlation_id: Mapped[str | None]
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    rn_status: Mapped[str] = mapped_column(default="internal")
    rn_sent_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(f"rn_status IN {RN_STATUSES}", name="rn_status_valid"),
        Index("ix_oversight_events_occurred_at_brin", "occurred_at", postgresql_using="brin"),
        Index("ix_oversight_events_object", "object_type", "object_id", "occurred_at"),
    )


class RiskIndicator(Base):
    """RI-01..15 (design/02 § oversight). See the module docstring for the
    harvest-vs-detect split. `idempotency_key` is UNIQUE and is what makes
    both the harvester and the RI-03 sweep safe to run on any schedule,
    any number of times, without a separate checkpoint table."""

    __tablename__ = "risk_indicators"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str]
    level: Mapped[str]
    object_type: Mapped[str | None]
    object_id: Mapped[uuid.UUID | None]
    responsible_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    description: Mapped[str]
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    idempotency_key: Mapped[uuid.UUID] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(default="new")
    rn_status: Mapped[str] = mapped_column(default="internal")
    rn_sent_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(f"code IN {RISK_INDICATOR_CODES}", name="code_valid"),
        CheckConstraint(f"level IN {RISK_INDICATOR_LEVELS}", name="level_valid"),
        CheckConstraint(f"status IN {RISK_INDICATOR_STATUSES}", name="status_valid"),
        CheckConstraint(f"rn_status IN {RN_STATUSES}", name="rn_status_valid"),
        Index("ix_risk_indicators_code_occurred_at", "code", "occurred_at"),
    )
