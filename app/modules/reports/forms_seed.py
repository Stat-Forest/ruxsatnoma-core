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
`"auto"` — computed from `permits`/`invoices`/`applicants`/`allocations`
(this module's reader join, `repo.report_rows`) or from `inspections` via
its own service (`inspection_result`, decision #105); `"manual"` — a hodim
types it (a registry field the join does not carry today, or a signature
slot that is not a data field at all). Every code is a snake_case name a
hodim/central admin would recognise from the printed form, not the bare
column number.
"""

from typing import Any

GRAZING_FORM_CODE = "2-ilova"
HAYMAKING_FORM_CODE = "3-ilova"

# tz/13 columns 1-11 are identical in shape between the two forms — the
# permit's own requisites plus the person it was issued to.
_SHARED_HEAD: list[dict[str, Any]] = [
    {
        "code": "permit_series_number",
        "label": {
            "uz_cyrl": "Рухсатнома серияси ва рақами",
            "uz_latn": "Ruxsatnoma seriyasi va raqami",
            "ru": "Серия и номер разрешения",
        },
        "source": "auto",
        "type": "text",
    },
    {
        "code": "legal_name",
        "label": {
            "uz_cyrl": "Юридик шахс номи",
            "uz_latn": "Yuridik shaxs nomi",
            "ru": "Название юрлица",
        },
        "source": "manual",
        "type": "text",
    },
    {
        "code": "legal_head_name",
        "label": {
            "uz_cyrl": "Юрлик раҳбари Ф.И.Ш.",
            "uz_latn": "Yurlik rahbari F.I.Sh.",
            "ru": "ФИО руководителя юрлица",
        },
        "source": "manual",
        "type": "text",
    },
    {
        "code": "legal_requisites",
        "label": {
            "uz_cyrl": "Юрлик реквизитлари",
            "uz_latn": "Yurlik rekvizitlari",
            "ru": "Реквизиты юрлица",
        },
        "source": "manual",
        "type": "text",
    },
    {
        "code": "legal_address",
        "label": {
            "uz_cyrl": "Юрлик манзили",
            "uz_latn": "Yurlik manzili",
            "ru": "Адрес юрлица",
        },
        "source": "manual",
        "type": "text",
    },
    {
        "code": "legal_stir",
        "label": {"uz_cyrl": "Юрлик СТИР", "uz_latn": "Yurlik STIR", "ru": "СТИР юрлица"},
        "source": "auto",
        "type": "text",
    },
    {
        "code": "individual_name",
        "label": {
            "uz_cyrl": "Жисмоний шахс Ф.И.Ш.",
            "uz_latn": "Jismoniy shaxs F.I.Sh.",
            "ru": "ФИО физлица",
        },
        "source": "auto",
        "type": "text",
    },
    {
        "code": "individual_address",
        "label": {
            "uz_cyrl": "Жисмоний шахс манзили",
            "uz_latn": "Jismoniy shaxs manzili",
            "ru": "Адрес физлица",
        },
        "source": "auto",
        "type": "text",
    },
    {
        "code": "individual_pinfl",
        "label": {"uz_cyrl": "ЖШШИР/СТИР", "uz_latn": "JSHSHIR/STIR", "ru": "СТИР/ЖШШИР физлица"},
        "source": "auto",
        "type": "text",
    },
    {
        "code": "individual_phone",
        "label": {
            "uz_cyrl": "Телефон рақами",
            "uz_latn": "Telefon raqami",
            "ru": "Телефон физлица",
        },
        "source": "auto",
        "type": "text",
    },
    {
        "code": "individual_passport",
        "label": {
            "uz_cyrl": "Паспорт маълумотлари",
            "uz_latn": "Pasport maʼlumotlari",
            "ru": "Паспортные данные физлица",
        },
        "source": "manual",
        "type": "text",
    },
]

_SHARED_TAIL: list[dict[str, Any]] = [
    {
        "code": "total_amount",
        "label": {
            "uz_cyrl": "Рухсатнома умумий суммаси",
            "uz_latn": "Ruxsatnoma umumiy summasi",
            "ru": "Общая сумма разрешения",
        },
        "source": "auto",
        "type": "money",
    },
    {
        "code": "paid_amount",
        "label": {
            "uz_cyrl": "Тўланган сумма ва санаси",
            "uz_latn": "Toʻlangan summa va sanasi",
            "ru": "Оплаченная сумма и дата оплаты",
        },
        "source": "auto",
        "type": "money",
    },
    # Decision #106: paid and refunded print as two separate figures, never
    # netted — a printed report for a past month must never change
    # retroactively. Not one of tz/13's original numbered columns; added by
    # this ruling, same shape as every other "auto" money column.
    {
        "code": "refunded_amount",
        "label": {
            "uz_cyrl": "Қайтарилган сумма",
            "uz_latn": "Qaytarilgan summa",
            "ru": "Возвращённая сумма",
        },
        "source": "auto",
        "type": "money",
    },
    {
        "code": "distribution",
        "label": {"uz_cyrl": "Тақсимот", "uz_latn": "Taqsimot", "ru": "Распределение"},
        "source": "auto",
        "type": "text",
    },
    {
        "code": "inspection_result",
        "label": {
            "uz_cyrl": "Текшириш натижаси",
            "uz_latn": "Tekshirish natijasi",
            "ru": "Результат инспекции",
        },
        "source": "auto",
        "type": "text",
    },
    {
        "code": "signature_applicant",
        "label": {
            "uz_cyrl": "Фойдаланувчи имзоси",
            "uz_latn": "Foydalanuvchi imzosi",
            "ru": "Подпись пользователя",
        },
        "source": "manual",
        "type": "text",
    },
    {
        "code": "signature_staff",
        "label": {
            "uz_cyrl": "Масъул ходим имзоси",
            "uz_latn": "Masʼul xodim imzosi",
            "ru": "Подпись ответственного сотрудника",
        },
        "source": "manual",
        "type": "text",
    },
]

GRAZING_COLUMNS: list[dict[str, Any]] = [
    *_SHARED_HEAD,
    {
        "code": "pasture_bulim",
        "label": {
            "uz_cyrl": "Ўрмон бўлими",
            "uz_latn": "Oʻrmon boʻlimi",
            "ru": "Расположение — бўлим",
        },
        "source": "manual",
        "type": "text",
    },
    {
        "code": "pasture_contour",
        "label": {"uz_cyrl": "Контур", "uz_latn": "Kontur", "ru": "Расположение — контур"},
        "source": "auto",
        "type": "text",
    },
    {
        "code": "pasture_massif",
        "label": {"uz_cyrl": "Массив", "uz_latn": "Massiv", "ru": "Расположение — массив"},
        "source": "manual",
        "type": "text",
    },
    {
        "code": "area_ha",
        "label": {
            "uz_cyrl": "Ажратилган майдон (га)",
            "uz_latn": "Ajratilgan maydon (ga)",
            "ru": "Выделенная площадь (га)",
        },
        "source": "auto",
        "type": "number",
    },
    {
        "code": "livestock_adult",
        "label": {
            "uz_cyrl": "Катта ёшдаги чорва",
            "uz_latn": "Katta yoshdagi chorva",
            "ru": "Скот взрослый",
        },
        "source": "auto",
        "type": "number",
    },
    {
        "code": "livestock_young",
        "label": {
            "uz_cyrl": "2 ёшгача бўлган чорва",
            "uz_latn": "2 yoshgacha boʻlgan chorva",
            "ru": "Скот до 2 лет",
        },
        "source": "auto",
        "type": "number",
    },
    {
        "code": "livestock_sheep_goat_6m",
        "label": {
            "uz_cyrl": "Қўй/эчки 6+ ой",
            "uz_latn": "Qoʻy/echki 6+ oy",
            "ru": "Овцы/козы 6+ мес",
        },
        "source": "auto",
        "type": "number",
    },
    {
        "code": "livestock_sheep_goat_under_6m",
        "label": {
            "uz_cyrl": "Қўй/эчки 6 ойгача",
            "uz_latn": "Qoʻy/echki 6 oygacha",
            "ru": "Овцы/козы до 6 мес",
        },
        "source": "auto",
        "type": "number",
    },
    {
        "code": "sb_load",
        "label": {
            "uz_cyrl": "Шартли бош юкламаси (СБ)",
            "uz_latn": "Shartli bosh yuklamasi (SB)",
            "ru": "Нагрузка СБ",
        },
        "source": "auto",
        "type": "number",
    },
    {
        "code": "period_from",
        "label": {
            "uz_cyrl": "Амал қилиш муддати — бошланиши",
            "uz_latn": "Amal qilish muddati — boshlanishi",
            "ru": "Срок действия — начало",
        },
        "source": "auto",
        "type": "date",
    },
    {
        "code": "period_to",
        "label": {
            "uz_cyrl": "Амал қилиш муддати — тугаши",
            "uz_latn": "Amal qilish muddati — tugashi",
            "ru": "Срок действия — конец",
        },
        "source": "auto",
        "type": "date",
    },
    *_SHARED_TAIL,
]

HAYMAKING_COLUMNS: list[dict[str, Any]] = [
    *_SHARED_HEAD,
    {
        "code": "hayfield_area_ha",
        "label": {
            "uz_cyrl": "Пичанзор майдони (га)",
            "uz_latn": "Pichanzor maydoni (ga)",
            "ru": "Площадь сенокоса (га)",
        },
        "source": "auto",
        "type": "number",
    },
    {
        "code": "hay_volume",
        "label": {
            "uz_cyrl": "Пичан ҳажми (тонна, м³)",
            "uz_latn": "Pichan hajmi (tonna, m³)",
            "ru": "Объём сена (тонны, м³)",
        },
        "source": "manual",
        "type": "number",
    },
    {
        "code": "contour_id",
        "label": {
            "uz_cyrl": "Контур/субконтур ID",
            "uz_latn": "Kontur/subkontur ID",
            "ru": "ID контура/субконтура",
        },
        "source": "auto",
        "type": "text",
    },
    {
        "code": "period_from",
        "label": {
            "uz_cyrl": "Амал қилиш муддати — бошланиши",
            "uz_latn": "Amal qilish muddati — boshlanishi",
            "ru": "Срок действия — начало",
        },
        "source": "auto",
        "type": "date",
    },
    {
        "code": "period_to",
        "label": {
            "uz_cyrl": "Амал қилиш муддати — тугаши",
            "uz_latn": "Amal qilish muddati — tugashi",
            "ru": "Срок действия — конец",
        },
        "source": "auto",
        "type": "date",
    },
    *_SHARED_TAIL,
]

assert len(GRAZING_COLUMNS) == 29, "tz/13's 28 columns + refunded_amount (decision #106)"
assert len(HAYMAKING_COLUMNS) == 23, "tz/13's 22 columns + refunded_amount (decision #106)"
