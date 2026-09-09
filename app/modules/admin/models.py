"""admin module models (design/02 § admin): territories, organization hierarchy,
hard catalogs and soft classifiers.

Nothing here is ever deleted — `status='archived'` (design/02 principle 7). Codes are
stable ASCII slugs; the official СОАТО identifier is a separate nullable column filled
at stage 7.2 (ruling 4). `system_settings` is NOT here — it lives in app/core/models.py
(ruling 8).
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

ORGANIZATION_KINDS = ("agency", "territorial", "leshoz", "bolim", "aylanma", "bolak")
QUANTITY_UNITS = ("head", "ton", "hive", "ha", "person_day", "m3", "unit")


class Region(Base):
    """14 fixed regions (12 viloyat + Karakalpakstan + Tashkent city); seeded by 0005."""

    __tablename__ = "regions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(unique=True)  # stable slug (ruling 4)
    soato_code: Mapped[str | None] = mapped_column(unique=True)  # official id, filled at 7.2
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sort_order: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class District(Base):
    """~208 districts/cities of regional subordination; loaded by the seed CLI (ruling 5)."""

    __tablename__ = "districts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(unique=True)
    soato_code: Mapped[str | None] = mapped_column(unique=True)
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    region_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regions.id"))
    sort_order: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (Index("ix_districts_region", "region_id"),)


class Organization(Base):
    """agency → territorial → leshoz → bolim → aylanma → bolak (ruling 6).

    `requisites` holds bank details, including the leshoz's own account
    `payments` resolves for its remainder share of an invoice (design/02;
    stage 7.9 decision #154 replaced the original fixed 50/50 split with a
    configurable receivers directory — see `payments.models.Allocation`'s
    own docstring). Kind-pair validation and cycle checks live in the
    service — they span two rows, which a CHECK cannot see.
    """

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id"))
    kind: Mapped[str]
    code: Mapped[str] = mapped_column(unique=True)
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    stir: Mapped[str | None]
    region_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("regions.id"))
    district_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("districts.id"))
    requisites: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(default="active")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "kind IN ('agency', 'territorial', 'leshoz', 'bolim', 'aylanma', 'bolak')",
            name="kind_valid",
        ),
        CheckConstraint("status IN ('active', 'archived')", name="status_valid"),
        CheckConstraint(r"stir IS NULL OR stir ~ '^\d{9}$'", name="stir_format"),
        # The agency is the only root, and it is always a root (ruling 6).
        CheckConstraint("(kind = 'agency') = (parent_id IS NULL)", name="root_is_agency"),
        Index("ix_organizations_parent", "parent_id"),
        Index("ix_organizations_region", "region_id"),
        Index("ix_organizations_district", "district_id"),
        # At most one agency row: unique over a constant-valued subset.
        Index(
            "uq_organizations_single_agency",
            "kind",
            unique=True,
            postgresql_where=text("kind = 'agency'"),
        ),
    )


class Classifier(Base):
    """Soft-dictionary catalog: doc_types, benefit_categories, rejection_reasons, …"""

    __tablename__ = "classifiers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(unique=True)
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ClassifierItem(Base):
    """A versioned dictionary value: superseding = archive + insert (ruling 7)."""

    __tablename__ = "classifier_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    classifier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("classifiers.id"))
    code: Mapped[str]
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    props: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    valid_from: Mapped[date]
    valid_to: Mapped[date | None]
    sort_order: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="active")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="status_valid"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="valid_period"),
        Index("ix_classifier_items_classifier", "classifier_id"),
        Index(
            "uq_classifier_items_active_code",
            "classifier_id",
            "code",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class ActivityType(Base):
    """The 6 forest-use activities (hard catalog). `quantity_unit` says what the
    tariff's `quantity` counts — provisional until VMQ 278 arrives (ruling 14)."""

    __tablename__ = "activity_types"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(unique=True)
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    quantity_unit: Mapped[str]
    sort_order: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="active")
    # Ruling #138: what the public site SHOWS about this service. `description` is
    # LocalizedName-shaped (uz_latn required, decision #90); NULL is legal so a row
    # added later is not blocked on copy being written.
    description: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Ruling #138a: DISPLAY ONLY. The enforced deadline is
    # `applications.service.SLA_DAYS` and nothing here may be read in its place.
    processing_days: Mapped[int] = mapped_column(default=15, server_default="15")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "quantity_unit IN ('head', 'ton', 'hive', 'ha', 'person_day', 'm3', 'unit')",
            name="quantity_unit_valid",
        ),
        CheckConstraint("status IN ('active', 'archived')", name="status_valid"),
        CheckConstraint("processing_days > 0", name="processing_days_positive"),
    )


class LivestockType(Base):
    """Livestock species × age group (tz/06). The CoefSB coefficients are NOT here —
    they are versioned in `rule_parameters` (stage 3.7)."""

    __tablename__ = "livestock_types"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(unique=True)
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sort_order: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="active")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (CheckConstraint("status IN ('active', 'archived')", name="status_valid"),)


class Announcement(Base):
    """Multilingual targeted announcements (tz/02: admin module owns them).
    audience: {"role_codes": [...], "region_ids": [...]} — a missing key or null
    audience means no restriction on that axis (ruling 12). `public_on_landing`
    is the separate, narrower door: the anonymous public site (`0037`)."""

    __tablename__ = "announcements"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    title: Mapped[dict[str, Any]] = mapped_column(JSONB)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    audience: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    publish_from: Mapped[datetime | None]
    publish_to: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(default="draft")
    # `0037`: the row is also readable by anonymous `landing` traffic. Only ever
    # set on an announcement with no `audience` — a targeted notice is addressed
    # at staff, and the service refuses the combination outright.
    public_on_landing: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('draft', 'published', 'archived')", name="status_valid"),
        Index("ix_announcements_status_window", "status", "publish_from"),
        Index(
            "ix_announcements_landing",
            "publish_from",
            postgresql_where=text("public_on_landing"),
        ),
    )


class AnnouncementFile(Base):
    """M2M announcements ↔ media_files (design/02)."""

    __tablename__ = "announcement_files"

    announcement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("announcements.id"), primary_key=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("media_files.id"), primary_key=True)
    position: Mapped[int] = mapped_column(default=0)


class LegalDocument(Base):
    """The public register of legal acts behind `landing`'s /documents page
    (`0043`), which until now was four rows hard-coded in the front-end under a
    Download button wired to nothing.

    Deliberately not an `Announcement` carrying a flag: a news item has an
    audience and a publication window, an act has a number and a date of
    adoption, and one table would leave both rows carrying the other's dead
    columns (plan 07.8 ruling R1).

    `file_id` is ONE file, not the M2M `announcement_files` set — an act on this
    page is one PDF. `source_url` (lex.uz) is what a row offers until that PDF
    exists, and `legal_documents_service.publish` refuses a row that has
    neither: a published row whose button opens nothing is precisely the broken
    page this register replaces.
    """

    __tablename__ = "legal_documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    title: Mapped[dict[str, Any]] = mapped_column(JSONB)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    doc_number: Mapped[str]
    adopted_on: Mapped[date]
    source_url: Mapped[str | None]
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"))
    status: Mapped[str] = mapped_column(default="draft", server_default=text("'draft'"))
    # The Forest Code has to head the page forever, and by date of adoption it is
    # the oldest row in the register: the list orders by this first (ruling R4).
    sort_order: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('draft', 'published', 'archived')", name="status_valid"),
        # The anonymous list is the only query internet traffic runs, and the
        # published rows are a small subset of the table: partial, in the order
        # that query reads them.
        Index(
            "ix_legal_documents_public",
            "sort_order",
            "adopted_on",
            postgresql_where=text("status = 'published'"),
        ),
    )
