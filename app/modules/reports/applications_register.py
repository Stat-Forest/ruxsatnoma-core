"""`GET /applications/export.xlsx` — the applications register on paper, in
the columns the Agency's PM asked for (Odilxon, 2026-09-13): region,
district, leshoz, contour, the applicant and their phone, the activity, the
benefit, quantity and area (a grazing herd also kind by kind), the filing
date, the permit's issue date and term, the calculated and the paid amount,
the status — as the customer's six groups AND as our exact one — and the
inspector's conclusion from the site visit (the newest SIGNED field act on
the filing, C6).

It lives in `reports`, not `applications`, because half of those columns
come from `permits` and `payments` (level 4) and `applications` (level 3)
may not read them; a reader module may (design/01 rule 5). The SCOPE is
still `applications.service.list_applications` — own rows ∪ zone, the same
call the screen makes (ruling #204 R2), with the cap as the page size — and
every id the rows carry is resolved into a name in ONE query per table.
Column headers are in the adminka's own words (`src/i18n/uz_latn.ts`,
`ru.ts`) so the file reads like the screen; `ru` is the administrative
second language (ruling R5).
"""

import uuid
from collections.abc import Callable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.core.schemas import PageParams
from app.modules.admin import service as admin_service
from app.modules.applications import service as applications_service
from app.modules.applications.models import Application
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.inspections import service as inspections_service
from app.modules.reports import repo

STATUS_LABELS: dict[str, dict[str, str]] = {
    "SUBMITTED": {"uz_latn": "Yuborilgan", "ru": "Подана"},
    "IN_REVIEW": {"uz_latn": "Koʻrib chiqilmoqda", "ru": "На рассмотрении"},
    "PENDING_INFO": {"uz_latn": "Maʼlumot kutilmoqda", "ru": "Ожидает сведений"},
    "RETURNED": {"uz_latn": "Tuzatishga qaytarilgan", "ru": "Возвращена на доработку"},
    "APPROVED": {"uz_latn": "Tasdiqlangan", "ru": "Одобрена"},
    "INVOICED": {"uz_latn": "Hisob-faktura yuborilgan", "ru": "Выставлен счёт"},
    "PAID": {"uz_latn": "Toʻlangan", "ru": "Оплачена"},
    "PERMIT_ISSUED": {"uz_latn": "Ruxsatnoma berilgan", "ru": "Разрешение выдано"},
    "REJECTED": {"uz_latn": "Rad etilgan", "ru": "Отклонена"},
    "CANCELLED": {"uz_latn": "Bekor qilingan", "ru": "Отменена"},
    "EXPIRED_UNPAID": {"uz_latn": "Toʻlanmay muddati oʻtgan", "ru": "Просрочена без оплаты"},
    "CLOSED": {"uz_latn": "Yopilgan", "ru": "Закрыта"},
    "ARCHIVED": {"uz_latn": "Arxivlangan", "ru": "В архиве"},
}

# The customer's six-word vocabulary over our thirteen statuses. Each of ours
# falls into exactly one group (a guard test holds this against
# `APPLICATION_STATUSES`); the exact label prints in the column beside it, so
# «returned for rework» and «awaiting information» do not vanish into one
# word.
STATUS_GROUPS: dict[str, str] = {
    "SUBMITTED": "new",
    "IN_REVIEW": "not_reviewed",
    "PENDING_INFO": "not_reviewed",
    "RETURNED": "not_reviewed",
    "APPROVED": "reviewed",
    "INVOICED": "reviewed",
    "PAID": "reviewed",
    "PERMIT_ISSUED": "permit_issued",
    "REJECTED": "rejected",
    "CANCELLED": "rejected",
    "EXPIRED_UNPAID": "rejected",
    "CLOSED": "permit_expired",
    "ARCHIVED": "permit_expired",
}
GROUP_LABELS: dict[str, dict[str, str]] = {
    "new": {"uz_latn": "Yangi ariza", "ru": "Новая заявка"},
    "not_reviewed": {"uz_latn": "Koʻrib chiqilmagan", "ru": "Не рассмотрена"},
    "reviewed": {"uz_latn": "Koʻrib chiqilgan", "ru": "Рассмотрена"},
    "rejected": {"uz_latn": "Rad etilgan", "ru": "Отклонена"},
    "permit_issued": {"uz_latn": "Ruxsatnoma berilgan", "ru": "Разрешение выдано"},
    "permit_expired": {
        "uz_latn": "Ruxsatnoma muddati tugagan",
        "ru": "Срок разрешения истёк",
    },
}
# `activity_types.quantity_unit` → the word the adminka prints for it
# (`norms.tariffs.quantityUnit.*`).
UNIT_LABELS: dict[str, dict[str, str]] = {
    "head": {"uz_latn": "bosh", "ru": "голова"},
    "ton": {"uz_latn": "tonna", "ru": "тонна"},
    "hive": {"uz_latn": "ari uyasi", "ru": "улей"},
    "ha": {"uz_latn": "ga", "ru": "га"},
    "person_day": {"uz_latn": "kishi-kun", "ru": "человеко-день"},
    "m3": {"uz_latn": "m³", "ru": "м³"},
    "unit": {"uz_latn": "dona", "ru": "штука"},
}
# `inspection_acts.result` → the adminka's word (`inspector.actForm.result.*`);
# printed as the conclusion when the act carries no notes of its own.
RESULT_LABELS: dict[str, dict[str, str]] = {
    "compliant": {"uz_latn": "Mos", "ru": "Соответствует"},
    "warning": {"uz_latn": "Eslatma", "ru": "Замечание"},
    "violation": {"uz_latn": "Buzilish", "ru": "Нарушение"},
}
TITLE = {"uz_latn": "Arizalar", "ru": "Заявки"}
FILENAME_STEM = "arizalar"


def _label(table: dict[str, dict[str, str]], code: str, lang: xlsx.Lang) -> str:
    """An unknown code renders as itself — never an empty cell that hides it."""
    return table.get(code, {}).get(lang, code)


class Row:
    """One application plus everything the sheet shows beside it, resolved
    once per table rather than once per row. `n` is the serial number of
    the row in THIS file (the customer's «тартиб рақами»), not an identity."""

    def __init__(
        self,
        n: int,
        app: Application,
        *,
        region: str,
        district: str,
        organization: str,
        contour: str,
        applicant: str,
        phone: str | None,
        activity_type: str,
        quantity: Decimal | int | None,
        unit: str,
        herd: str | None,
        benefit: str | None,
        permit: tuple[date, date, datetime | None] | None,
        calculated: Decimal | None,
        paid: Decimal | None,
        conclusion: str | None,
    ) -> None:
        self.n = n
        self.app = app
        self.id = app.id
        self.region = region
        self.district = district
        self.organization = organization
        self.contour = contour
        self.applicant = applicant
        self.phone = phone
        self.activity_type = activity_type
        self.quantity = quantity
        self.unit = unit
        self.herd = herd
        self.benefit = benefit
        self.permit_from = permit[0] if permit else None
        self.permit_to = permit[1] if permit else None
        self.issued_at = permit[2] if permit else None
        self.calculated = calculated
        self.paid = paid
        self.conclusion = conclusion


def _conclusion(acts: Sequence[Any], lang: xlsx.Lang) -> str | None:
    """The inspector's word on the filing: the NEWEST signed act's notes, or
    its result as a word when the inspector wrote none; `None` — an empty
    cell — while nobody has been out. The list is chronological
    (`acts_for_applications`), so the newest is the last."""
    if not acts:
        return None
    act = acts[-1]
    if act.notes:
        return act.notes
    return _label(RESULT_LABELS, act.result, lang) if act.result else None


def _herd(herd: Sequence[tuple[dict[str, Any], int]] | None, lang: xlsx.Lang) -> str | None:
    """«Qoramol (katta): 2, Qoʻy va echki (6 oydan katta): 40» — what the
    single number in «quantity» is made of; `None`, an empty cell, for an
    activity that declares no herd."""
    if not herd:
        return None
    return ", ".join(f"{xlsx.localized(name, lang)}: {heads}" for name, heads in herd)


def _attr(name: str) -> Callable[[Row], xlsx.CellValue]:
    return lambda row: getattr(row.app, name)


def columns(lang: xlsx.Lang) -> list[xlsx.Column[Row]]:
    """The customer's order: place → who → what → when → money → status →
    conclusion; the application's own number second, the id last (R4)."""
    return [
        xlsx.Column("n", {"uz_latn": "№", "ru": "№"}, lambda r: r.n, 6),
        xlsx.Column(
            "number", {"uz_latn": "Ariza raqami", "ru": "Номер заявки"}, _attr("number"), 18
        ),
        xlsx.Column("region", {"uz_latn": "Viloyat", "ru": "Область"}, lambda r: r.region, 20),
        xlsx.Column("district", {"uz_latn": "Tuman", "ru": "Район"}, lambda r: r.district, 20),
        xlsx.Column(
            "organization",
            {"uz_latn": "Oʻrmon xoʻjaligi", "ru": "Лесхоз"},
            lambda r: r.organization,
            30,
        ),
        xlsx.Column("contour", {"uz_latn": "Kontur", "ru": "Контур"}, lambda r: r.contour, 14),
        xlsx.Column("applicant", {"uz_latn": "F.I.Sh.", "ru": "ФИО"}, lambda r: r.applicant, 30),
        xlsx.Column("phone", {"uz_latn": "Telefon", "ru": "Телефон"}, lambda r: r.phone, 16),
        xlsx.Column(
            "activity_type",
            {"uz_latn": "Ruxsatnoma turi", "ru": "Вид разрешения"},
            lambda r: r.activity_type,
            24,
        ),
        xlsx.Column(
            "benefit", {"uz_latn": "Imtiyoz turi", "ru": "Вид льготы"}, lambda r: r.benefit, 24
        ),
        xlsx.Column(
            "quantity", {"uz_latn": "Miqdor", "ru": "Количество"}, lambda r: r.quantity, 12
        ),
        xlsx.Column("unit", {"uz_latn": "Birlik", "ru": "Ед. изм."}, lambda r: r.unit, 12),
        xlsx.Column("herd", {"uz_latn": "Chorva", "ru": "Скот"}, lambda r: r.herd, 40),
        xlsx.Column(
            "requested_area_ha",
            {"uz_latn": "Maydon, ga", "ru": "Площадь, га"},
            _attr("requested_area_ha"),
            12,
        ),
        xlsx.Column(
            "submitted_at",
            {"uz_latn": "Ariza kelib tushgan sana", "ru": "Дата поступления заявки"},
            _attr("submitted_at"),
            18,
        ),
        xlsx.Column(
            "issued_at",
            {"uz_latn": "Ruxsatnoma berilgan sana", "ru": "Дата выдачи разрешения"},
            lambda r: r.issued_at,
            18,
        ),
        xlsx.Column(
            "permit_from",
            {"uz_latn": "Ruxsatnoma muddati: dan", "ru": "Срок разрешения: с"},
            lambda r: r.permit_from,
            12,
        ),
        xlsx.Column(
            "permit_to",
            {"uz_latn": "Ruxsatnoma muddati: gacha", "ru": "Срок разрешения: по"},
            lambda r: r.permit_to,
            12,
        ),
        xlsx.Column(
            "period_from",
            {"uz_latn": "Soʻralgan davr: dan", "ru": "Запрошенный период: с"},
            _attr("period_from"),
            12,
        ),
        xlsx.Column(
            "period_to",
            {"uz_latn": "Soʻralgan davr: gacha", "ru": "Запрошенный период: по"},
            _attr("period_to"),
            12,
        ),
        xlsx.Column(
            "calculated",
            {"uz_latn": "Hisoblangan summa", "ru": "Начисленная сумма"},
            lambda r: r.calculated,
            16,
        ),
        xlsx.Column(
            "paid", {"uz_latn": "Toʻlangan summa", "ru": "Оплаченная сумма"}, lambda r: r.paid, 16
        ),
        xlsx.Column(
            "status_group",
            {"uz_latn": "Ariza holati", "ru": "Статус заявки"},
            lambda r: _label(GROUP_LABELS, STATUS_GROUPS.get(r.app.status, r.app.status), lang),
            26,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(STATUS_LABELS, r.app.status, lang),
            26,
        ),
        xlsx.Column(
            "conclusion",
            {"uz_latn": "Xulosa", "ru": "Заключение"},
            lambda r: r.conclusion,
            60,
        ),
        xlsx.id_column(),
    ]


async def rows(
    db: AsyncSession,
    *,
    actor: User,
    lang: xlsx.Lang,
    status: str | None,
    activity_type_id: uuid.UUID | None,
    contour_id: uuid.UUID | None,
    applicant_id: uuid.UUID | None,
    number: str | None,
    period_from: date | None,
    period_to: date | None,
    created_from: date | None,
    created_to: date | None,
) -> tuple[list[Row], int, int]:
    """`(rows, total, cap)`. `PageParams.model_construct` bypasses the model's
    own `page_size <= 100` — the export is the one caller legitimately above
    it, and the cap (ruling R3) is what bounds it instead."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    apps, total = await applications_service.list_applications(
        db,
        actor=actor,
        params=PageParams.model_construct(page=1, page_size=cap),
        status=status,
        activity_type_id=activity_type_id,
        contour_id=contour_id,
        applicant_id=applicant_id,
        number=number,
        period_from=period_from,
        period_to=period_to,
        created_from=created_from,
        created_to=created_to,
    )
    ids = {a.id for a in apps}
    contacts = await auth_service.applicant_contacts(db, {a.applicant_id for a in apps})
    places = await admin_service.organization_places(
        db, {a.assigned_org_id for a in apps if a.assigned_org_id is not None}
    )
    activities: dict[uuid.UUID, dict[str, Any]] = await admin_service.activity_type_names(db)
    units = await admin_service.activity_type_units(db)
    benefits = await admin_service.classifier_item_names(
        db, {a.benefit_category_item_id for a in apps if a.benefit_category_item_id is not None}
    )
    contours = await gis_service.contour_numbers_by_ids(
        db, {a.contour_id for a in apps if a.contour_id is not None}
    )
    herds = await repo.herd_by_application(db, ids)
    permits = await repo.permit_facts_by_application(db, ids)
    calculated = await repo.calculated_amount_by_application(db, ids)
    paid = await repo.paid_amount_by_application(db, ids)
    acts = await inspections_service.acts_for_applications(db, list(ids))

    out: list[Row] = []
    for n, app in enumerate(apps, start=1):
        name, phone = contacts.get(app.applicant_id, ("", None))
        org_name, region, district = (
            places.get(app.assigned_org_id, ({}, None, None))
            if app.assigned_org_id is not None
            else ({}, None, None)
        )
        out.append(
            Row(
                n,
                app,
                region=xlsx.localized(region, lang),
                district=xlsx.localized(district, lang),
                organization=xlsx.localized(org_name, lang),
                contour=contours.get(app.contour_id, "") if app.contour_id is not None else "",
                applicant=name,
                phone=phone,
                activity_type=(
                    xlsx.localized(activities.get(app.activity_type_id), lang)
                    if app.activity_type_id is not None
                    else ""
                ),
                # Grazing counts its herd in the items and leaves `quantity`
                # NULL; every other activity declares `quantity` itself.
                quantity=(
                    app.quantity
                    if app.quantity is not None or app.id not in herds
                    else sum(heads for _, heads in herds[app.id])
                ),
                unit=(
                    _label(UNIT_LABELS, units[app.activity_type_id], lang)
                    if app.activity_type_id is not None and app.activity_type_id in units
                    else ""
                ),
                herd=_herd(herds.get(app.id), lang),
                benefit=(
                    xlsx.localized(benefits.get(app.benefit_category_item_id), lang)
                    if app.benefit_category_item_id is not None
                    else None
                ),
                permit=permits.get(app.id),
                calculated=calculated.get(app.id),
                paid=paid.get(app.id),
                conclusion=_conclusion(acts.get(app.id, []), lang),
            )
        )
    return out, total, cap


def render(items: Sequence[Row], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, columns(lang), lang=lang, title=TITLE[lang])
