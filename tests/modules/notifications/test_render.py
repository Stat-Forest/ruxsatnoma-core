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


# Display formatting: a citizen reads `1 250 000` and `15.09.2026`, never the
# machine forms `1250000.00` / `2026-09-15` that `str()` produces — the dev
# stand's own SMS read "To'lov: 594000000.00 so'm, muddat 2026-09-20" before this.


def test_decimal_amount_is_grouped_by_thousands_without_empty_tiyin():
    from decimal import Decimal

    body = {"uz_latn": "{amount} so'm"}
    assert service.render(body, {"amount": Decimal("1250000.00")}, "uz_latn") == "1 250 000 so'm"


def test_decimal_amount_with_tiyin_keeps_two_places_behind_a_comma():
    from decimal import Decimal

    body = {"uz_latn": "{amount} so'm"}
    assert service.render(body, {"amount": Decimal("1234.5")}, "uz_latn") == "1 234,50 so'm"


def test_date_is_day_month_year():
    from datetime import date

    body = {"uz_latn": "{due_date} gacha"}
    assert service.render(body, {"due_date": date(2026, 9, 5)}, "uz_latn") == "05.09.2026 gacha"


def test_datetime_is_shown_as_tashkent_wall_clock():
    from datetime import UTC, datetime

    body = {"uz_latn": "{at}"}
    # 20:30 UTC is already the next calendar day in Tashkent (UTC+5).
    at = datetime(2026, 9, 15, 20, 30, tzinfo=UTC)
    assert service.render(body, {"at": at}, "uz_latn") == "16.09.2026 01:30"


def test_grouping_uses_a_plain_space_so_the_sms_stays_gsm_03_38():
    from decimal import Decimal

    text = service.render({"uz_latn": "{amount}"}, {"amount": Decimal("1000")}, "uz_latn")
    assert text == "1 000"
    assert " " not in text
