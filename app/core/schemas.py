"""API primitives shared by every module: localized names and the page envelope."""

from typing import Annotated, Self

from fastapi import Query
from pydantic import BaseModel, RootModel, model_validator

# design/02 principle 3: uz_cyrl is the fallback language and is always required.
LOCALES = ("uz_cyrl", "uz_latn", "ru", "kaa", "en")


class LocalizedName(RootModel[dict[str, str]]):
    """`{"uz_cyrl": "Номи", "ru": "Название"}` — validated, not free-form jsonb."""

    @model_validator(mode="after")
    def _check(self) -> Self:
        unknown = set(self.root) - set(LOCALES)
        if unknown:
            raise ValueError(f"unknown locales: {sorted(unknown)}")
        if not self.root.get("uz_cyrl", "").strip():
            raise ValueError("uz_cyrl is required and must not be blank")
        return self


class Page[T](BaseModel):
    """design/03 pagination envelope; `page_size` is capped by PageParams."""

    items: list[T]
    total: int
    page: int
    page_size: int


class PageParams(BaseModel):
    """Query dependency: `?page=1&page_size=20`, page_size max 100 (design/03)."""

    page: Annotated[int, Query(ge=1)] = 1
    page_size: Annotated[int, Query(ge=1, le=100)] = 20

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size
