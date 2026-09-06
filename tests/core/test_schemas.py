"""Shared API primitives: localized names (ruling 13) and the page envelope (ruling 11)."""

import pytest
from pydantic import BaseModel, ValidationError

from app.core.schemas import LocalizedName, Page


class Holder(BaseModel):
    name: LocalizedName


def test_localized_name_accepts_known_locales():
    # Pydantic validates/coerces the dict into LocalizedName at runtime; pyright's
    # dataclass_transform constructor typing has no way to see that (general pydantic
    # v2 limitation, not specific to this type — same pattern as tests/test_config.py).
    holder = Holder(
        name={"uz_latn": "Nomi", "ru": "Название", "en": "Name"}  # pyright: ignore
    )
    assert holder.name.root["uz_latn"] == "Nomi"


def test_localized_name_accepts_uz_latn_alone_with_no_uz_cyrl():
    # Decision #90: uz_cyrl is now optional — a name with only uz_latn is valid.
    holder = Holder(name={"uz_latn": "Nomi"})  # pyright: ignore[reportArgumentType]
    assert "uz_cyrl" not in holder.name.root


def test_localized_name_requires_uz_latn():
    with pytest.raises(ValidationError):
        Holder(name={"uz_cyrl": "Номи"})  # pyright: ignore[reportArgumentType]


def test_localized_name_rejects_empty_fallback():
    with pytest.raises(ValidationError):
        Holder(name={"uz_latn": "   "})  # pyright: ignore[reportArgumentType]


def test_localized_name_rejects_unknown_locale():
    with pytest.raises(ValidationError):
        Holder(name={"uz_latn": "Nomi", "fr": "Nom"})  # pyright: ignore[reportArgumentType]


def test_page_envelope_shape():
    page = Page[int](items=[1, 2], total=7, page=2, page_size=2)
    assert page.model_dump() == {"items": [1, 2], "total": 7, "page": 2, "page_size": 2}
