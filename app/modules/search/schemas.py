"""API shapes for `search`."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Spelled out rather than `Literal[*models.SEARCH_KINDS]` (pyright rejects a
# starred variable inside `Literal`, `reportInvalidTypeForm` — same shape
# `permits.schemas` documents for its own status literal).
# `test_models.py::test_search_kind_literal_matches_the_check_constraint`
# holds this against `models.SEARCH_KINDS`.
SearchKind = Literal["applications", "permits"]

_Name = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]


class SearchResultOut(BaseModel):
    """One row of a search result page — the fields common to every `kind`,
    never the full record: a search hit is a pointer for the client to open
    the real card through that domain's own route (`GET /applications/{id}`,
    `GET /permits/{id}`), which independently re-checks what this endpoint's
    zone filter already narrowed."""

    kind: SearchKind
    id: uuid.UUID
    number: str | None
    status: str
    organization_id: uuid.UUID | None
    applicant_name: str | None
    created_at: datetime


class SavedFilterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: _Name
    kind: SearchKind
    params: dict[str, Any] = Field(default_factory=dict)
    shared: dict[str, list[str]] | None = None


class SavedFilterPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: _Name | None = None
    params: dict[str, Any] | None = None
    shared: dict[str, list[str]] | None = None


class SavedFilterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    kind: SearchKind
    params: dict[str, Any]
    shared: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
