"""Norms, tariffs, rule parameters and calculations (design/02 § norms, corrected
by plan 03.7 against the primary sources VMQ 278 and VMQ 689).

Three invariants live in migration 0011 rather than here, because SQLAlchemy's
metadata cannot express them and Alembic does not diff them: the EXCLUDE
constraints that forbid two published rows in force at once, and the append-only
trigger on `calculations`. They are documented on each class below."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7
from app.modules.admin.models import QUANTITY_UNITS

PARAM_STATUSES = ("draft", "published", "archived")
NORM_STATUSES = ("draft", "review", "approved", "published", "archived")
# VMQ 278 charges grazing by four coarse groups, not by our ten livestock types
# (ruling 9); the mapping lives in the `tariff_group:<code>` rule parameters.
LIVESTOCK_GROUPS = ("large_adult", "large_young", "small_adult", "small_young")


class RuleParameter(Base):
    """Every number the engine uses, versioned by effective period (decision #10).

    Migration 0011 adds, and this class cannot express:
      EXCLUDE USING gist (code WITH =, daterange(effective_from, effective_to, '[]') WITH &&)
      WHERE (status = 'published')
    """

    __tablename__ = "rule_parameters"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str]
    value: Mapped[Any] = mapped_column(JSONB)
    unit: Mapped[str | None]
    effective_from: Mapped[date]
    effective_to: Mapped[date | None]
    basis: Mapped[str]
    status: Mapped[str] = mapped_column(default="draft")
    # NULL means "seeded by a migration" — an installation act, not a user action;
    # the maker-checker CHECK below exempts exactly those rows (Task 2).
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(f"status IN {PARAM_STATUSES}", name="status_valid"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="period_valid"
        ),
        CheckConstraint(
            "status <> 'published' OR created_by IS NULL "
            "OR (approved_by IS NOT NULL AND approved_by <> created_by)",
            name="maker_checker",
        ),
        Index("ix_rule_parameters_lookup", "code", "status", "effective_from"),
    )


class Tariff(Base):
    """VMQ 278 rates. `livestock_group` is set for grazing only (ruling 9);
    `benefit_modifiers` maps a `benefit_categories` classifier code to a
    multiplier on the coefficient (ruling 20).

    Migration 0011 adds:
      EXCLUDE USING gist (activity_type_id WITH =,
                          COALESCE(livestock_group, '') WITH =,
                          daterange(effective_from, effective_to, '[]') WITH &&)
      WHERE (status = 'published')
    """

    __tablename__ = "tariffs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    activity_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("activity_types.id"))
    livestock_group: Mapped[str | None]
    coefficient: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    quantity_unit: Mapped[str]
    benefit_modifiers: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    effective_from: Mapped[date]
    effective_to: Mapped[date | None]
    basis: Mapped[str]
    status: Mapped[str] = mapped_column(default="draft")
    # NULL means "seeded by a migration" — an installation act, not a user action;
    # the maker-checker CHECK below exempts exactly those rows (Task 2).
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(f"status IN {PARAM_STATUSES}", name="status_valid"),
        CheckConstraint(
            f"livestock_group IS NULL OR livestock_group IN {LIVESTOCK_GROUPS}",
            name="livestock_group_valid",
        ),
        CheckConstraint("coefficient >= 0", name="coefficient_non_negative"),
        # Added by 0013 (review finding I8): `quantity_unit` had no bound at
        # all on this table, unlike its sibling `activity_types.quantity_unit`
        # — any string was storable and was copied verbatim into an immutable
        # calculation's `breakdown`. Derived from `admin.models.QUANTITY_UNITS`
        # rather than retyped (lesson: constraint strings duplicated in Python
        # tuples are two sources of truth).
        CheckConstraint(f"quantity_unit IN {QUANTITY_UNITS}", name="quantity_unit_valid"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="period_valid"
        ),
        CheckConstraint(
            "status <> 'published' OR created_by IS NULL "
            "OR (approved_by IS NOT NULL AND approved_by <> created_by)",
            name="maker_checker",
        ),
        Index("ix_tariffs_lookup", "activity_type_id", "status", "effective_from"),
    )


class Norm(Base):
    """The norm for one contour × activity (VMQ 689). `max_sb` is computed at
    publication from the contour's published area and frozen (ruling 17).

    Migration 0011 adds:
      EXCLUDE USING gist (contour_id WITH =, activity_type_id WITH =,
                          daterange(effective_from, effective_to, '[]') WITH &&)
      WHERE (status = 'published')
    """

    __tablename__ = "norms"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    contour_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contours.id"))
    activity_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("activity_types.id"))
    yield_c_per_ha: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    season: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    rotation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    max_sb: Mapped[int | None]
    # Ruling #176: capacity generalises `max_sb` to every non-grazing activity,
    # expressed in that activity's own `activity_types.quantity_unit` (ha for
    # haymaking, hive for an apiary, m3 for deadwood, person_day for
    # recreation). Grazing does NOT use this column — its capacity IS `max_sb`
    # above, and a second field would be a second source of truth for the same
    # fact (`service.create_norm`/`update_norm` refuse a grazing norm that sets
    # it). NULL is not "no limit": `checks._limit_check` treats an absent
    # capacity (no norm at all, or this column left NULL) as EXCLUSIVE for the
    # requested period, never as unlimited.
    capacity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    geobotanic_doc_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"))
    approval_doc_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"))
    effective_from: Mapped[date]
    effective_to: Mapped[date | None]
    status: Mapped[str] = mapped_column(default="draft")
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    published_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(f"status IN {NORM_STATUSES}", name="status_valid"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="period_valid"
        ),
        CheckConstraint("yield_c_per_ha IS NULL OR yield_c_per_ha >= 0", name="yield_valid"),
        CheckConstraint("capacity IS NULL OR capacity >= 0", name="capacity_valid"),
        CheckConstraint(
            "status <> 'published' OR approval_doc_id IS NOT NULL", name="published_needs_doc"
        ),
        Index("ix_norms_lookup", "contour_id", "activity_type_id", "status", "effective_from"),
    )


class ActivitySeason(Base):
    """Ruling #177 (stage 9): the season windows and minimum term for a WHOLE
    leshoz × activity, rather than repeated on every one of its contours — a
    leshoz with 151 contours used to have to state its grazing season 151
    times, and stated it nowhere at all where no geobotanical survey exists
    and therefore no `Norm` can be published.

    `season` carries the exact same JSONB shape `Norm.season` already uses
    (`schemas.Season`'s edge validation applies here too, via
    `schemas.ActivitySeasonIn`/`ActivitySeasonPatch` — the same malformed-
    window guard `checks._in_window` already reads defensively). Unlike
    `Norm`, there is no lifecycle here (draft/review/approved/…): this is a
    plain, current-value setting the leshoz or the central office edits in
    place, not a versioned catalog row.

    `checks._season_check` prefers a contour's OWN norm windows when it has
    any; this row is the fallback resolved when it does not
    (`checks.resolve_effective_windows`). `min_term_days` has NO norm-level
    override — ruling #177 only speaks of overriding the WINDOWS — so
    `checks._min_term_check` reads this table alone."""

    __tablename__ = "activity_seasons"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    activity_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("activity_types.id"))
    season: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    min_term_days: Mapped[int | None]
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "activity_type_id", name="uq_activity_seasons_org_activity"
        ),
        CheckConstraint("min_term_days IS NULL OR min_term_days > 0", name="min_term_days_valid"),
    )


class Calculation(Base):
    """An immutable calculation (ruling 21 — migration 0011 adds the
    BEFORE UPDATE/DELETE/TRUNCATE trigger). `application_id`'s FK to `applications`
    is added by migration 0015, `NOT VALID` then `VALIDATE CONSTRAINT` (the table
    already has rows), the same way 3.2a closed `audit_log.user_id`."""

    __tablename__ = "calculations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    application_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("applications.id"))
    contour_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contours.id"))
    activity_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("activity_types.id"))
    rule_code_version: Mapped[str]
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    used_sb: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    max_sb: Mapped[int | None]
    remaining_sb: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    breakdown: Mapped[Any] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        Index("ix_calculations_application", "application_id", "created_at"),
    )
