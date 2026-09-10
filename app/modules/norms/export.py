"""`GET /norms/export.xlsx`, `GET /tariffs/export.xlsx`,
`GET /rule-parameters/export.xlsx` (stage 13, ruling #204): the three
registers on paper.

Every `rows_*` function calls the SAME repo function its list route calls,
with the same filters, so the export can never show a row the screen would
not (ruling R2) — this module has no zone concept of its own to get wrong:
all three list routes are open to any authenticated user (see
`router.py`/`refs_router.py`'s own module docstrings), so there is no scope
to widen in the first place. Every id the sheet shows is resolved to a name
in ONE batch query per table; column headers and status labels are copied
from the adminka's `norms/*Tab.tsx` and `i18n/*.ts` so the file reads like
the screen."""

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.modules.admin import service as admin_service
from app.modules.gis import service as gis_service
from app.modules.norms import repo
from app.modules.norms.models import Norm, Tariff

# --- shared status vocabulary ------------------------------------------------
# `norms`'s own five-stage lifecycle (draft/review/approved/published/
# archived, `adminka/src/i18n/*.ts` keys `norms.norms.status.*`) is a
# DIFFERENT vocabulary from the maker-checker three (draft/published/
# archived, `norms.tariffs.status.*`/`norms.params.status.*`) that
# `rows_tariffs`/`rows_parameters` below use — the latter two happen to
# read identically, so one table serves both.

NORM_STATUS_LABELS: dict[str, dict[str, str]] = {
    "draft": {"uz_latn": "Qoralama", "ru": "Черновик"},
    "review": {"uz_latn": "Koʻrib chiqilmoqda", "ru": "На рассмотрении"},
    "approved": {"uz_latn": "Kelishildi", "ru": "Согласована"},
    "published": {"uz_latn": "Eʼlon qilingan", "ru": "Опубликована"},
    "archived": {"uz_latn": "Arxivlangan", "ru": "В архиве"},
}
VERSIONED_STATUS_LABELS: dict[str, dict[str, str]] = {
    "draft": {"uz_latn": "Qoralama", "ru": "Черновик"},
    "published": {"uz_latn": "Eʼlon qilingan", "ru": "Опубликовано"},
    "archived": {"uz_latn": "Arxivlangan", "ru": "В архиве"},
}


def _label(table: dict[str, dict[str, str]], code: str, lang: xlsx.Lang) -> str:
    """An unknown code renders as itself — never an empty cell that hides
    it (same rule Track A's `applications/export.py` states for its own
    status column)."""
    return table.get(code, {}).get(lang, code)


# --- norms -------------------------------------------------------------------

NORMS_TITLE = {"uz_latn": "Normalar", "ru": "Нормы"}


class NormRow:
    """One norm plus the names the sheet shows; columns read attributes off
    this, so the batch resolvers run once per table, not once per row."""

    def __init__(self, norm: Norm, *, contour: str, activity_type: str) -> None:
        self.norm = norm
        self.id = norm.id
        self.contour = contour
        self.activity_type = activity_type


def norms_columns(lang: xlsx.Lang) -> list[xlsx.Column[NormRow]]:
    a = lambda f: lambda r: getattr(r.norm, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column("contour", {"uz_latn": "Kontur", "ru": "Контур"}, lambda r: r.contour, 14),
        xlsx.Column(
            "activity_type",
            {"uz_latn": "Faoliyat turi", "ru": "Вид деятельности"},
            lambda r: r.activity_type,
            24,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(NORM_STATUS_LABELS, r.norm.status, lang),
            20,
        ),
        xlsx.Column(
            "yield_c_per_ha",
            {"uz_latn": "Hosildorlik, s/ga", "ru": "Урожайность, ц/га"},
            a("yield_c_per_ha"),
            16,
        ),
        xlsx.Column(
            "max_sb",
            {"uz_latn": "Limit, shartli bosh", "ru": "Лимит, усл. голов"},
            a("max_sb"),
            16,
        ),
        xlsx.Column(
            "capacity",
            {"uz_latn": "Sigʻim (boshqa faoliyat)", "ru": "Ёмкость (прочие виды)"},
            a("capacity"),
            18,
        ),
        xlsx.Column(
            "effective_from",
            {"uz_latn": "Amal qilish boshlanishi", "ru": "Действует с"},
            a("effective_from"),
            16,
        ),
        xlsx.Column(
            "effective_to",
            {"uz_latn": "Amal qilish tugashi", "ru": "Действует по"},
            a("effective_to"),
            16,
        ),
        xlsx.Column(
            "published_at",
            {"uz_latn": "Eʼlon qilingan", "ru": "Опубликована"},
            a("published_at"),
            18,
        ),
        xlsx.Column("created_at", {"uz_latn": "Yaratilgan", "ru": "Создана"}, a("created_at"), 18),
        xlsx.id_column(),
    ]


async def rows_norms(
    db: AsyncSession,
    *,
    lang: xlsx.Lang,
    contour_id: uuid.UUID | None,
    activity_type_id: uuid.UUID | None,
    status: str | None,
) -> tuple[list[NormRow], int, int]:
    """(rows, total, cap). `repo.list_norms` is exactly what `GET /norms`
    itself calls (`router.py::list_norms`) — this module has no service-level
    wrapper for it, so the export reaches the same repo function rather than
    inventing a layer the list route does not have."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    norms, total = await repo.list_norms(
        db,
        contour_id=contour_id,
        activity_type_id=activity_type_id,
        status=status,
        limit=cap,
        offset=0,
    )
    contours = await gis_service.contour_numbers_by_ids(
        db, {n.contour_id for n in norms if n.contour_id}
    )
    activities = await admin_service.activity_type_names(db)
    return (
        [
            NormRow(
                norm,
                contour=contours.get(norm.contour_id, ""),
                activity_type=xlsx.localized(activities.get(norm.activity_type_id), lang),
            )
            for norm in norms
        ],
        total,
        cap,
    )


def render_norms(items: Sequence[NormRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, norms_columns(lang), lang=lang, title=NORMS_TITLE[lang])


# --- tariffs -------------------------------------------------------------------

TARIFFS_TITLE = {"uz_latn": "Tariflar", "ru": "Тарифы"}

LIVESTOCK_GROUP_LABELS: dict[str, dict[str, str]] = {
    "large_adult": {"uz_latn": "Yirik chorva, katta", "ru": "Крупный скот, взрослый"},
    "large_young": {"uz_latn": "Yirik chorva, yosh", "ru": "Крупный скот, молодняк"},
    "small_adult": {"uz_latn": "Mayda chorva, katta", "ru": "Мелкий скот, взрослый"},
    "small_young": {"uz_latn": "Mayda chorva, yosh", "ru": "Мелкий скот, молодняк"},
}
QUANTITY_UNIT_LABELS: dict[str, dict[str, str]] = {
    "head": {"uz_latn": "bosh", "ru": "голова"},
    "ton": {"uz_latn": "tonna", "ru": "тонна"},
    "hive": {"uz_latn": "ari uyasi", "ru": "улей"},
    "ha": {"uz_latn": "ga", "ru": "га"},
    "person_day": {"uz_latn": "kishi-kun", "ru": "человеко-день"},
    "m3": {"uz_latn": "m³", "ru": "м³"},
    "unit": {"uz_latn": "dona", "ru": "штука"},
}


def _benefit_modifiers_cell(value: dict[str, Any] | None) -> str:
    """Flattened the same way `TariffsTab.tsx`'s own column renders it
    (`code: modifier` pills) — a spreadsheet cell has no pills, so this joins
    them with a comma."""
    if not value:
        return ""
    return ", ".join(f"{code}: {modifier}" for code, modifier in value.items())


class TariffRow:
    def __init__(self, tariff: Tariff, *, activity_type: str) -> None:
        self.tariff = tariff
        self.id = tariff.id
        self.activity_type = activity_type


def tariffs_columns(lang: xlsx.Lang) -> list[xlsx.Column[TariffRow]]:
    a = lambda f: lambda r: getattr(r.tariff, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column(
            "activity_type",
            {"uz_latn": "Faoliyat turi", "ru": "Вид деятельности"},
            lambda r: r.activity_type,
            24,
        ),
        xlsx.Column(
            "livestock_group",
            {"uz_latn": "Chorva guruhi", "ru": "Группа скота"},
            lambda r: (
                _label(LIVESTOCK_GROUP_LABELS, r.tariff.livestock_group, lang)
                if r.tariff.livestock_group
                else ""
            ),
            20,
        ),
        xlsx.Column(
            "coefficient", {"uz_latn": "Koeffitsient", "ru": "Коэффициент"}, a("coefficient"), 14
        ),
        xlsx.Column(
            "quantity_unit",
            {"uz_latn": "Oʻlchov birligi", "ru": "Единица"},
            lambda r: _label(QUANTITY_UNIT_LABELS, r.tariff.quantity_unit, lang),
            14,
        ),
        xlsx.Column(
            "benefit_modifiers",
            {"uz_latn": "Imtiyozlar", "ru": "Льготы"},
            lambda r: _benefit_modifiers_cell(r.tariff.benefit_modifiers),
            24,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(VERSIONED_STATUS_LABELS, r.tariff.status, lang),
            16,
        ),
        xlsx.Column(
            "effective_from",
            {"uz_latn": "Amal qilish boshlanishi", "ru": "Действует с"},
            a("effective_from"),
            16,
        ),
        xlsx.Column(
            "effective_to",
            {"uz_latn": "Amal qilish tugashi", "ru": "Действует по"},
            a("effective_to"),
            16,
        ),
        xlsx.Column("basis", {"uz_latn": "Asos", "ru": "Основание"}, a("basis"), 30),
        xlsx.id_column(),
    ]


async def rows_tariffs(
    db: AsyncSession,
    *,
    lang: xlsx.Lang,
    activity_type_id: uuid.UUID | None,
    on_date: date | None,
    status: str | None,
) -> tuple[list[TariffRow], int, int]:
    """`activity_type_id` here is already RESOLVED (the router does the
    `activity_code` -> `activity_type_id` lookup with its own
    `_resolve_activity_type_id`, exactly once, the same as `list_tariffs`
    does) — this function does not repeat that resolution."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    tariffs, total = await repo.list_tariffs(
        db,
        activity_type_id=activity_type_id,
        on_date=on_date,
        status=status,
        limit=cap,
        offset=0,
    )
    activities = await admin_service.activity_type_names(db)
    return (
        [
            TariffRow(
                tariff, activity_type=xlsx.localized(activities.get(tariff.activity_type_id), lang)
            )
            for tariff in tariffs
        ],
        total,
        cap,
    )


def render_tariffs(items: Sequence[TariffRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, tariffs_columns(lang), lang=lang, title=TARIFFS_TITLE[lang])
