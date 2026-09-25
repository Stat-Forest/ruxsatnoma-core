"""API shapes for `search`."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from app.core.schemas import LIST_MAX_ITEMS, CodeStr, JsonObject

# Spelled out rather than `Literal[*models.SEARCH_KINDS]` (pyright rejects a
# starred variable inside `Literal`, `reportInvalidTypeForm` — same shape
# `permits.schemas` documents for its own status literal).
# `test_models.py::test_search_kind_literal_matches_the_check_constraint`
# holds this against `models.SEARCH_KINDS`.
SearchKind = Literal["applications", "permits"]

# Same reasoning, against `models.EXPORT_FORMATS` /
# `models.EXPORT_STATUSES` — `test_models.py`'s guard holds both.
ExportFormat = Literal["pdf", "xlsx"]
ExportStatus = Literal["done", "failed"]

_Name = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
_Query = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]

# `SavedFilterIn.shared`/`.shared.*`/`.shared.*[]` (stage 17 C1, enforced I1
# final review): the stored shape is `{"role_codes": [...], "user_ids":
# [...]}` (`search.repo`'s own docstring) — role codes and stringified user
# UUIDs both fit `CodeStr`'s bound, and those two are the ONLY keys
# `service.py` ever reads (`_visible_to`). `Field(max_length=2)` on the dict
# both emits AND enforces `maxProperties` (pydantic), unlike the
# `json_schema_extra` this replaces, which only ever documented it.
#
# The key type stays plain `str` rather than `Literal["role_codes",
# "user_ids"]` — tried, and it broke `search.service.update_saved_filter`'s
# `row.shared = patch.shared`: `SavedFilter.shared` is a
# `Mapped[dict[str, Any] | None]` JSONB column, and pyright's standard-mode
# generics are invariant on a dict's KEY type, so `dict[Literal[...], V]` is
# not assignable to `dict[str, Any]` even though every Literal member is a
# `str`. `_reject_unknown_keys` below enforces the same restriction as a
# value-level check instead, and `propertyNames` is declared explicitly
# alongside `max_length` so the walker still sees a bounded typed dict.
_SharedGroup = Annotated[list[CodeStr], Field(max_length=LIST_MAX_ITEMS)]
_SHARED_KEYS = ("role_codes", "user_ids")


def _reject_unknown_shared_keys(value: dict[str, list[str]]) -> dict[str, list[str]]:
    unknown = set(value) - set(_SHARED_KEYS)
    if unknown:
        raise ValueError(f"unknown shared keys: {sorted(unknown)}")
    return value


SharedMap = Annotated[
    dict[str, _SharedGroup],
    Field(
        max_length=len(_SHARED_KEYS),
        json_schema_extra={"propertyNames": {"enum": list(_SHARED_KEYS)}},
    ),
    AfterValidator(_reject_unknown_shared_keys),
]


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
    params: JsonObject = Field(default_factory=dict)
    shared: SharedMap | None = None


class SavedFilterPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: _Name | None = None
    params: JsonObject | None = None
    shared: SharedMap | None = None


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


class ExportCreate(BaseModel):
    """`POST /search/exports` — the same filters `GET /search` accepts for
    one `kind`, plus the output `format`. No `page`/`page_size`: an export is
    not paged, it is capped (`search_export_max_rows`, ruling #20)."""

    model_config = ConfigDict(extra="forbid")

    kind: SearchKind
    format: ExportFormat
    q: _Query | None = None
    status: CodeStr | None = None
    organization_id: uuid.UUID | None = None
    activity_type_id: uuid.UUID | None = None
    series: Annotated[str | None, StringConstraints(max_length=10)] = None


class ExportJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    kind: SearchKind
    format: ExportFormat
    params: dict[str, Any]
    status: ExportStatus
    file_id: uuid.UUID | None
    row_count: int | None
    total_matched: int | None
    watermarked: bool
    error: str | None
    created_at: datetime
    finished_at: datetime
