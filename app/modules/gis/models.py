"""Spatial core (design/02 § gis, corrected by plan 03.6a). Geometry lives ONLY in
contour_versions.geom and layer_features.geom; a `contours` row is identity. All
geometry is WGS84 (EPSG:4326, decision #13); areas are computed over `geography`
and stored in hectares (ruling 2)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

# The catalogue of tz/07 — a fixed list, not admin CRUD (ruling 19).
LAYER_CODES = (
    "forest_fund",
    "org_boundaries",
    "contours",
    "pastures",
    "hayfields",
    "apiaries",
    "recreation",
    "restrictions",
    "protection",
    "rotation",
    "rest_calendar",
    "water_points",
    "cattle_corridors",
    "fire_bans",
    "special_areas",
)
# Layers whose intersection with a contour is reported by the checks (ruling 15).
RESTRICTION_LAYER_CODES = ("restrictions", "protection", "fire_bans")

VERSION_STATUSES = ("draft", "review", "approved", "published", "archived")
FEATURE_STATUSES = ("draft", "published", "archived")
IMPORT_STATUSES = ("pending", "processing", "review", "approved", "done", "failed")
IMPORT_FORMATS = ("shp", "geojson", "kml", "kmz", "gpkg", "csv", "zip")
GEOMETRY_SOURCES = ("cadastre", "survey", "aerial", "gps", "import")

# The one layer whose features are contours (identity + versioned geometry);
# every other layer's features are `layer_features` rows. Lives here rather
# than in a service so `service` and `import_service` share ONE definition.
CONTOUR_LAYER_CODE = "contours"


class GisLayer(Base):
    """The 15 layers of tz/07, seeded by migration 0010 with fixed ids."""

    __tablename__ = "gis_layers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(unique=True)
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    geometry_type: Mapped[str]
    is_public: Mapped[bool] = mapped_column(default=False)
    style: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(default="active")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="status_valid"),
        CheckConstraint(
            "geometry_type IN ('POINT', 'LINESTRING', 'POLYGON', 'MULTIPOLYGON', 'GEOMETRY')",
            name="geometry_type_valid",
        ),
    )


class Contour(Base):
    """Identity of a contour or sub-contour. The hierarchy is set by hand until the
    Agency delivers the contour layer itself (ruling 11)."""

    __tablename__ = "contours"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    layer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gis_layers.id"), index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contours.id"), index=True)
    kind: Mapped[str] = mapped_column(default="contour")
    number: Mapped[str]
    status: Mapped[str] = mapped_column(default="active")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('contour', 'subcontour')", name="kind_valid"),
        CheckConstraint("status IN ('active', 'archived')", name="status_valid"),
        CheckConstraint("parent_id IS NULL OR kind = 'subcontour'", name="parent_needs_subcontour"),
        UniqueConstraint("organization_id", "number", name="uq_contour_number"),
    )


class ContourVersion(Base):
    """Versioned geometry. Exactly one published version per contour (partial unique);
    an application/permit freezes `contour_version_id`, so republishing never moves
    the ground under an issued permit."""

    __tablename__ = "contour_versions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    contour_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contours.id"), index=True)
    version_no: Mapped[int] = mapped_column(default=1)
    geom: Mapped[Any] = mapped_column(
        Geometry("MULTIPOLYGON", srid=4326, spatial_index=True, nullable=False)
    )
    area_ha: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    declared_area_ha: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    source: Mapped[str]
    accuracy_m: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    survey_date: Mapped[date | None] = mapped_column()
    effective_from: Mapped[date | None] = mapped_column()
    effective_to: Mapped[date | None] = mapped_column()
    approval_doc_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"))
    import_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("gis_imports.id"), index=True)
    status: Mapped[str] = mapped_column(default="draft")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    published_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'review', 'approved', 'published', 'archived')",
            name="status_valid",
        ),
        CheckConstraint(
            "source IN ('cadastre', 'survey', 'aerial', 'gps', 'import')", name="source_valid"
        ),
        CheckConstraint("area_ha > 0", name="area_positive"),
        CheckConstraint(
            "status <> 'published' OR approval_doc_id IS NOT NULL", name="published_needs_doc"
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name="effective_period_valid",
        ),
        UniqueConstraint("contour_id", "version_no", name="uq_contour_version_no"),
        Index(
            "uq_contour_published_version",
            "contour_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
    )


class LayerFeature(Base):
    """The objects of every layer other than contours: fund boundaries, protection
    zones, fire bans (a period plus a territory), rotation, water points."""

    __tablename__ = "layer_features"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    layer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gis_layers.id"), index=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    geom: Mapped[Any] = mapped_column(
        Geometry("GEOMETRY", srid=4326, spatial_index=True, nullable=False)
    )
    name: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    props: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    valid_from: Mapped[date | None] = mapped_column()
    valid_to: Mapped[date | None] = mapped_column()
    approval_doc_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"))
    import_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("gis_imports.id"), index=True)
    status: Mapped[str] = mapped_column(default="draft")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('draft', 'published', 'archived')", name="status_valid"),
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from",
            name="validity_period_valid",
        ),
    )


class GisImport(Base):
    """The import journal AND the batch's state machine (ruling 3/6): one row per
    uploaded file, claimed by the job with FOR UPDATE SKIP LOCKED."""

    __tablename__ = "gis_imports"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    layer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gis_layers.id"), index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("media_files.id"))
    approval_doc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("media_files.id"))
    format: Mapped[str]
    attribute_map: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(default="pending")
    error_report: Mapped[list[Any] | None] = mapped_column(JSONB)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    finished_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'review', 'approved', 'done', 'failed')",
            name="status_valid",
        ),
        CheckConstraint(
            "format IN ('shp', 'geojson', 'kml', 'kmz', 'gpkg', 'csv', 'zip')", name="format_valid"
        ),
        Index("ix_gis_imports_pending", "status", "created_at"),
    )
