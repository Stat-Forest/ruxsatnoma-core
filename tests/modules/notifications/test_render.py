"""Template rendering (ruling 9): a regex substitution, not str.format — an
admin-authored template must never reach Python attributes, and a missing
placeholder must never raise inside a business transaction."""

from app.modules.notifications import service

BODY = {
    "uz_cyrl": "Ариза {application_number} қабул қилинди",
    "uz_latn": "Ariza {application_number} qabul qilindi",
    "ru": "Заявка {application_number} принята",
}


def test_renders_in_the_requested_language():
    assert service.render(BODY, {"application_number": "RX-1"}, "ru") == "Заявка RX-1 принята"


def test_falls_back_to_uz_latn_when_the_language_is_absent():
    # decision #90: FALLBACK_LANGUAGE is uz_latn, the one language every
    # LocalizedName is guaranteed to carry — not uz_cyrl, which is now optional.
    assert service.render(BODY, {"application_number": "RX-1"}, "en").startswith("Ariza RX-1")


def test_missing_placeholder_is_left_literal_and_does_not_raise():
    assert service.render(BODY, {}, "ru") == "Заявка {application_number} принята"


def test_attribute_access_is_not_a_placeholder():
    body = {"uz_cyrl": "{x.__class__} {X} { spaced }"}
    assert service.render(body, {"x": "v"}, "uz_cyrl") == "{x.__class__} {X} { spaced }"


def test_non_string_params_are_stringified():
    body = {"uz_cyrl": "{amount} сўм"}
    assert service.render(body, {"amount": 12000}, "uz_cyrl") == "12000 сўм"
