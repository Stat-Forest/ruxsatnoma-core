"""The shared bounded request types (stage 17, QA run 01 classes C1/C2).

Tested once, here: every module's schema reuses these, and
`tests/test_request_bounds.py` guards that every request field does."""

import pytest
from pydantic import BaseModel, ValidationError

from app.core.schemas import (
    CODE_MAX_LENGTH,
    JSON_MAX_BYTES,
    LONG_TEXT_MAX_LENGTH,
    CodeStr,
    JsonObject,
    JsonValue,
    LocalizedName,
    NoteStr,
    PasswordStr,
    TextStr,
    UrlStr,
)


class Body(BaseModel):
    code: CodeStr | None = None
    text: TextStr | None = None
    note: NoteStr | None = None
    password: PasswordStr | None = None
    url: UrlStr | None = None
    extra: JsonObject | None = None
    name: LocalizedName | None = None
    setting: JsonValue = None


def test_codes_are_stripped_so_a_trailing_space_is_the_same_code() -> None:
    assert Body(code="  LZ-01 ").code == "LZ-01"


@pytest.mark.parametrize("field", ["code", "text"])
def test_whitespace_only_is_refused_where_text_is_required(field: str) -> None:
    with pytest.raises(ValidationError):
        Body.model_validate({field: "   "})


def test_an_optional_note_still_accepts_blank_and_stores_it_stripped() -> None:
    assert Body(note="   ").note == ""


def test_a_value_one_past_the_limit_is_refused_and_the_limit_itself_passes() -> None:
    assert Body(code="x" * CODE_MAX_LENGTH).code is not None
    with pytest.raises(ValidationError):
        Body(code="x" * (CODE_MAX_LENGTH + 1))


def test_passwords_are_never_stripped() -> None:
    assert Body(password=" secret ").password == " secret "


def test_a_url_must_be_http_or_https() -> None:
    assert Body(url="https://lex.uz/docs/1").url == "https://lex.uz/docs/1"
    with pytest.raises(ValidationError):
        Body(url="not-a-url-at-all")


def test_a_json_object_over_the_byte_cap_is_refused() -> None:
    assert Body(extra={"k": "v"}).extra == {"k": "v"}
    with pytest.raises(ValidationError):
        Body(extra={"k": "v" * JSON_MAX_BYTES})


def test_localized_name_values_are_capped() -> None:
    with pytest.raises(ValidationError):
        Body.model_validate({"name": {"uz_latn": "x" * (LONG_TEXT_MAX_LENGTH + 1)}})


def test_a_json_value_accepts_a_bare_scalar_and_caps_an_oversize_one() -> None:
    """Unlike `JsonObject`, a setting's value is not always a `dict` — most of
    `settings_store.SETTING_SPECS` are `int`/`str`/`bool` (stage 17 t6)."""
    assert Body(setting=45).setting == 45
    with pytest.raises(ValidationError):
        Body(setting="x" * JSON_MAX_BYTES)
