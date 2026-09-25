"""API primitives shared by every module: bounded request types, localized names
and the page envelope."""

import json
from typing import Annotated, Any, Self

from fastapi import Query
from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    RootModel,
    StringConstraints,
    model_validator,
)

# Uzbek Latin is the system's base language (decision #18); every other one falls
# back to it, and the public site renders in it. uz_cyrl is optional.
LOCALES = ("uz_cyrl", "uz_latn", "ru", "kaa", "en")

# --- Request bounds (stage 17, QA run 01 C1/C2) -------------------------------
# Every string, list and number in a REQUEST body carries an upper bound;
# `tests/test_request_bounds.py` fails CI on one that does not. Every column is
# TEXT, so an unbounded value is never a 500 — it is storage, a broken table
# layout and CPU proportional to its size (the QA run's 50 000-item estimate).
CODE_MAX_LENGTH = 64
NAME_MAX_LENGTH = 255
TEXT_MAX_LENGTH = 2000
LONG_TEXT_MAX_LENGTH = 10_000
PASSWORD_MAX_LENGTH = 128
BLOB_MAX_LENGTH = 1_048_576  # base64 PKCS#7 with its certificate chain
URL_MAX_LENGTH = 2048
LIST_MAX_ITEMS = 100
JSON_MAX_BYTES = 65_536
SORT_ORDER_MAX = 10_000
DAYS_MAX = 3650


def _text(max_length: int, *, blank: bool = False) -> StringConstraints:
    # Stripped before the length checks run, so "   " fails min_length=1 (C2)
    # and "LZ-01 " is the same code as "LZ-01" to a uniqueness check.
    return StringConstraints(
        strip_whitespace=True, min_length=0 if blank else 1, max_length=max_length
    )


CodeStr = Annotated[str, _text(CODE_MAX_LENGTH)]
NameStr = Annotated[str, _text(NAME_MAX_LENGTH)]
TextStr = Annotated[str, _text(TEXT_MAX_LENGTH)]
LongTextStr = Annotated[str, _text(LONG_TEXT_MAX_LENGTH)]
NoteStr = Annotated[str, _text(TEXT_MAX_LENGTH, blank=True)]
# Never stripped: a password's spaces are part of it.
PasswordStr = Annotated[str, StringConstraints(min_length=1, max_length=PASSWORD_MAX_LENGTH)]
BlobStr = Annotated[str, StringConstraints(min_length=1, max_length=BLOB_MAX_LENGTH)]
UrlStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=URL_MAX_LENGTH, pattern=r"^https?://\S+$"),
]


def _cap_json(value: dict[str, Any]) -> dict[str, Any]:
    size = len(json.dumps(value, default=str, separators=(",", ":")).encode())
    if size > JSON_MAX_BYTES:
        raise ValueError(f"object is {size} bytes, the limit is {JSON_MAX_BYTES}")
    return value


# A free-form object declares its bound as an `x-max-*` schema extension, which
# is what the convention test accepts in place of maxLength/maxItems.
JsonObject = Annotated[
    dict[str, Any],
    AfterValidator(_cap_json),
    Field(json_schema_extra={"x-max-json-bytes": JSON_MAX_BYTES}),
]


def _cap_json_value(value: Any) -> Any:
    size = len(json.dumps(value, default=str, separators=(",", ":")).encode())
    if size > JSON_MAX_BYTES:
        raise ValueError(f"value is {size} bytes, the limit is {JSON_MAX_BYTES}")
    return value


# Any JSON value (scalar, list or object) under the same byte cap as JsonObject.
JsonValue = Annotated[
    Any,
    AfterValidator(_cap_json_value),
    Field(json_schema_extra={"x-max-json-bytes": JSON_MAX_BYTES}),
]

LocalizedText = Annotated[str, StringConstraints(max_length=LONG_TEXT_MAX_LENGTH)]

# `Field(max_length=...)` on the dict itself (I1, final review): pydantic
# emits AND enforces `maxProperties` from this, unlike the `json_schema_extra`
# this replaces, which only ever emitted it. `propertyNames` is declared the
# same explicit way, NOT via a `Literal` key type: `LocalizedName.root` is
# assigned straight into a dozen `Mapped[dict[str, Any]]` JSONB columns
# (`ActivityType.name`, `Role.name`/`.description`, `FaqItem.question`, …),
# and a `dict[Literal[...], V]` is not assignable to `dict[str, Any]` under
# pyright's standard-mode invariant generics — tried, and it broke 17 call
# sites across 7 modules for a distinction pydantic's `RootModel` can only
# express at the type-checker level, not at the JSON-schema level anyway.
# The `_check` validator below already enforces the SAME set (`LOCALES`) at
# runtime, so `propertyNames` here documents exactly what that validator
# checks, together give the walker everything a `Literal` key type would.
_LocalizedNameRoot = Annotated[
    dict[str, LocalizedText],
    Field(max_length=len(LOCALES), json_schema_extra={"propertyNames": {"enum": list(LOCALES)}}),
]


class LocalizedName(RootModel[_LocalizedNameRoot]):
    """`{"uz_latn": "Nomi", "ru": "Название"}` — validated, not free-form jsonb.

    Decision #90: this used to require `uz_cyrl` and not `uz_latn` at all, which
    directly contradicted the already-merged announcements form (it required
    `uz_latn`) and, as data, left `gis_layers` with no Latin name for any of its
    fifteen rows. The flip could not land alone — every row written under the old
    rule has `uz_cyrl` and had no guarantee of `uz_latn` — so migration `0032`
    backfills `uz_latn` from `uz_cyrl` everywhere first (deterministic: Uzbek
    Cyrillic -> Latin is a well-defined mapping) and this validator changes in
    its wake, in the same commit. A database migrated before `0032` will start
    rejecting writes to any row it did not cover — that migration's own docstring
    lists every column it backfills.

    Each value is capped at `LONG_TEXT_MAX_LENGTH` (stage 17 C1): this same type
    carries help answers and notification bodies, not just short names.
    """

    @model_validator(mode="after")
    def _check(self) -> Self:
        unknown = set(self.root) - set(LOCALES)
        if unknown:
            raise ValueError(f"unknown locales: {sorted(unknown)}")
        if not self.root.get("uz_latn", "").strip():
            raise ValueError("uz_latn is required and must not be blank")
        return self


class Page[T](BaseModel):
    """design/03 pagination envelope; `page_size` is capped by PageParams."""

    items: list[T]
    total: int
    page: int
    page_size: int


# The ceiling on `page` and on every bare `?offset=`. Not a product decision:
# both are bound into SQL as bigints — `offset` is `(page - 1) * page_size` — and
# an unbounded one arrives at asyncpg as `DataError: value out of int64 range`,
# a 500 for a query string anybody can type. Found by 3.11a task 5, whose own
# `?number=` had the identical hole, and kept closed by
# `tests/test_code_conventions.py::test_every_integer_query_parameter_carries_an_upper_bound`.
# A billion pages of 100 is 10^11 rows, past anything this system will hold.
PAGING_MAX = 10**9


class PageParams(BaseModel):
    """Query dependency: `?page=1&page_size=20`, page_size max 100 (design/03)."""

    page: Annotated[int, Query(ge=1, le=PAGING_MAX)] = 1
    page_size: Annotated[int, Query(ge=1, le=100)] = 20

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size
