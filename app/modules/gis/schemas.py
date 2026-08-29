"""API shapes. GeoJSON is accepted and returned as a plain dict validated by
PostGIS (ST_GeomFromGeoJSON raises on malformed input) — modelling every GeoJSON
variant in pydantic would duplicate a parser we already have in the database."""

import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.core.schemas import LocalizedName


class LayerOut(BaseModel):
    id: uuid.UUID
    code: str
    name: LocalizedName
    geometry_type: str
    is_public: bool
    style: dict[str, Any]
    status: str


class LayerList(BaseModel):
    items: list[LayerOut]


class LayerPatch(BaseModel):
    style: dict[str, Any] | None = None
    is_public: bool | None = None
    status: str | None = Field(default=None, pattern="^(active|archived)$")
