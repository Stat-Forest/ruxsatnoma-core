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
        name={"uz_cyrl": "Номи", "ru": "Название", "en": "Name"}  # pyright: ignore
    )
    assert holder.name.root["uz_cyrl"] == "Номи"


def test_localized_name_requires_uz_cyrl():
    with pytest.raises(ValidationError):
        Holder(name={"ru": "Название"})  # pyright: ignore[reportArgumentType]


def test_localized_name_rejects_empty_fallback():
    with pytest.raises(ValidationError):
        Holder(name={"uz_cyrl": "   "})  # pyright: ignore[reportArgumentType]


def test_localized_name_rejects_unknown_locale():
    with pytest.raises(ValidationError):
        Holder(name={"uz_cyrl": "Номи", "fr": "Nom"})  # pyright: ignore[reportArgumentType]


def test_page_envelope_shape():
    page = Page[int](items=[1, 2], total=7, page=2, page_size=2)
    assert page.model_dump() == {"items": [1, 2], "total": 7, "page": 2, "page_size": 2}
