"""The `columns=[...]` payloads for 2-ilova (grazing) and 3-ilova (haymaking),
straight out of `tz/13-formy.md`'s two numbered tables.

Deliberately NOT a migration seed (plan "scope cuts"): `report_forms` is a
versioned, admin-editable catalog — С20 says "Центр создаёт форму отчёта" —
and a literal migration row would fight the very central-admin workflow this
module exists to support. These constants are what a real
`POST /reports/forms` call for either form would carry; tests build both
forms by POSTing this payload, exactly as a central admin would through the
UI.

`source` says how `service.generate_report` fills a column's value:
`"auto"` — computed from `permits`/`invoices`/`applicants` (this module's
reader join, `repo.report_rows`); `"manual"` — a hodim types it (a
registry field the join does not carry today, or a signature slot that is
not a data field at all). Every code is a snake_case name a hodim/central
admin would recognise from the printed form, not the bare column number.
"""

from typing import Any

GRAZING_FORM_CODE = "2-ilova"
HAYMAKING_FORM_CODE = "3-ilova"

# tz/13 columns 1-11 are identical in shape between the two forms — the
# permit's own requisites plus the person it was issued to.
_SHARED_HEAD: list[dict[str, Any]] = [
    {
        "code": "permit_series_number",
        "label": {"uz_cyrl": "Рухсатнома серияси ва рақами", "ru": "Серия и номер разрешения"},
        "source": "auto",
        "type": "text",
    },
    {
        "code": "legal_name",
        "label": {"uz_cyrl": "Юридик шахс номи", "ru": "Название юрлица"},
        "source": "manual",
        "type": "text",
    },
    {
        "code": "legal_head_name",
        "label": {"uz_cyrl": "Юрлик раҳбари Ф.И.Ш.", "ru": "ФИО руководителя юрлица"},
        "source": "manual",
        "type": "text",
    },
    {
        "code": "legal_requisites",
        "label": {"uz_cyrl": "Юрлик реквизитлари", "ru": "Реквизиты юрлица"},
        "source": "manual",
        "type": "text",
    },
    {
        "code": "legal_address",
        "label": {"uz_cyrl": "Юрлик манзили", "ru": "Адрес юрлица"},
        "source": "manual",
        "type": "text",
    },
    {
        "code": "legal_stir",
        "label": {"uz_cyrl": "Юрлик СТИР", "ru": "СТИР юрлица"},
        "source": "auto",
        "type": "text",
    },
    {
        "code": "individual_name",
        "label": {"uz_cyrl": "Жисмоний шахс Ф.И.Ш.", "ru": "ФИО физлица"},
        "source": "auto",
        "type": "text",
    },
    {
        "code": "individual_address",
        "label": {"uz_cyrl": "Жисмоний шахс манзили", "ru": "Адрес физлица"},
        "source": "auto",
        "type": "text",
    },
    {
        "code": "individual_pinfl",
        "label": {"uz_cyrl": "ЖШШИР/СТИР", "ru": "СТИР/ЖШШИР физлица"},
        "source": "auto",
        "type": "text",
    },
    {
        "code": "individual_phone",
        "label": {"uz_cyrl": "Телефон рақами", "ru": "Телефон физлица"},
        "source": "auto",
        "type": "text",
    },
    {
        "code": "individual_passport",
        "label": {"uz_cyrl": "Паспорт маълумотлари", "ru": "Паспортные данные физлица"},
        "source": "manual",
        "type": "text",
    },
]

_SHARED_TAIL: list[dict[str, Any]] = [
    {
        "code": "total_amount",
        "label": {"uz_cyrl": "Рухсатнома умумий суммаси", "ru": "Общая сумма разрешения"},
        "source": "auto",
        "type": "money",
    },
    {
        "code": "paid_amount",
        "label": {"uz_cyrl": "Тўланган сумма ва санаси", "ru": "Оплаченная сумма и дата оплаты"},
        "source": "auto",
        "type": "money",
    },
    {
        "code": "distribution",
        "label": {"uz_cyrl": "Тақсимот", "ru": "Распределение"},
        "source": "auto",
        "type": "text",
    },
    {
        "code": "inspection_result",
        "label": {"uz_cyrl": "Текшириш натижаси", "ru": "Результат инспекции"},
        "source": "auto",
        "type": "text",
    },
    {
        "code": "signature_applicant",
        "label": {"uz_cyrl": "Фойдаланувчи имзоси", "ru": "Подпись пользователя"},
        "source": "manual",
        "type": "text",
    },
    {
        "code": "signature_staff",
        "label": {"uz_cyrl": "Масъул ходим имзоси", "ru": "Подпись ответственного сотрудника"},
        "source": "manual",
        "type": "text",
    },
]

GRAZING_COLUMNS: list[dict[str, Any]] = [
    *_SHARED_HEAD,
    {
        "code": "pasture_bulim",
        "label": {"uz_cyrl": "Ўрмон бўлими", "ru": "Расположение — бўлим"},
        "source": "manual",
        "type": "text",
    },
    {
        "code": "pasture_contour",
        "label": {"uz_cyrl": "Контур", "ru": "Расположение — контур"},
        "source": "auto",
        "type": "text",
    },
    {
        "code": "pasture_massif",
        "label": {"uz_cyrl": "Массив", "ru": "Расположение — массив"},
        "source": "manual",
        "type": "text",
    },
    {
        "code": "area_ha",
        "label": {"uz_cyrl": "Ажратилган майдон (га)", "ru": "Выделенная площадь (га)"},
        "source": "auto",
        "type": "number",
    },
    {
        "code": "livestock_adult",
        "label": {"uz_cyrl": "Катта ёшдаги чорва", "ru": "Скот взрослый"},
        "source": "auto",
        "type": "number",
    },
    {
        "code": "livestock_young",
        "label": {"uz_cyrl": "2 ёшгача бўлган чорва", "ru": "Скот до 2 лет"},
        "source": "auto",
        "type": "number",
    },
    {
        "code": "livestock_sheep_goat_6m",
        "label": {"uz_cyrl": "Қўй/эчки 6+ ой", "ru": "Овцы/козы 6+ мес"},
        "source": "auto",
        "type": "number",
    },
    {
        "code": "livestock_sheep_goat_under_6m",
        "label": {"uz_cyrl": "Қўй/эчки 6 ойгача", "ru": "Овцы/козы до 6 мес"},
        "source": "auto",
        "type": "number",
    },
    {
        "code": "sb_load",
        "label": {"uz_cyrl": "Шартли бош юкламаси (СБ)", "ru": "Нагрузка СБ"},
        "source": "auto",
        "type": "number",
    },
    {
        "code": "period_from",
        "label": {"uz_cyrl": "Амал қилиш муддати — бошланиши", "ru": "Срок действия — начало"},
        "source": "auto",
        "type": "date",
    },
    {
        "code": "period_to",
        "label": {"uz_cyrl": "Амал қилиш муддати — тугаши", "ru": "Срок действия — конец"},
        "source": "auto",
        "type": "date",
    },
    *_SHARED_TAIL,
]

HAYMAKING_COLUMNS: list[dict[str, Any]] = [
    *_SHARED_HEAD,
    {
        "code": "hayfield_area_ha",
        "label": {"uz_cyrl": "Пичанзор майдони (га)", "ru": "Площадь сенокоса (га)"},
        "source": "auto",
        "type": "number",
    },
    {
        "code": "hay_volume",
        "label": {"uz_cyrl": "Пичан ҳажми (тонна, м³)", "ru": "Объём сена (тонны, м³)"},
        "source": "manual",
        "type": "number",
    },
    {
        "code": "contour_id",
        "label": {"uz_cyrl": "Контур/субконтур ID", "ru": "ID контура/субконтура"},
        "source": "auto",
        "type": "text",
    },
    {
        "code": "period_from",
        "label": {"uz_cyrl": "Амал қилиш муддати — бошланиши", "ru": "Срок действия — начало"},
        "source": "auto",
        "type": "date",
    },
    {
        "code": "period_to",
        "label": {"uz_cyrl": "Амал қилиш муддати — тугаши", "ru": "Срок действия — конец"},
        "source": "auto",
        "type": "date",
    },
    *_SHARED_TAIL,
]

assert len(GRAZING_COLUMNS) == 28, "tz/13: 2-ilova has 28 columns"
assert len(HAYMAKING_COLUMNS) == 22, "tz/13: 3-ilova has 22 columns"
