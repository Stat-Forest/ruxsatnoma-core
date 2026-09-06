"""API primitives shared by every module: localized names and the page envelope."""

from typing import Annotated, Self

from fastapi import Query
from pydantic import BaseModel, RootModel, model_validator

# Uzbek Latin is the system's base language (decision #18); every other one falls
# back to it, and the public site renders in it. uz_cyrl is optional.
LOCALES = ("uz_cyrl", "uz_latn", "ru", "kaa", "en")


class LocalizedName(RootModel[dict[str, str]]):
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
