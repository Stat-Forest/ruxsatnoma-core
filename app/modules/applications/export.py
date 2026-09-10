"""`GET /applications/export.xlsx` (stage 13, ruling #204): the list on paper.

`rows()` calls `service.list_applications` — the same scope the screen gets
(ruling R2: own rows ∪ zone, never the repo) — with the cap as the page size,
then resolves every id the sheet shows into a name in ONE query per table.
Column headers and status labels are copied from the adminka
(`src/pages/staff/format.ts`, `ApplicationsListPage.tsx`) so the file reads
like the screen; `ru` is the administrative second language (ruling R5).
"""

import uuid
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.core.schemas import PageParams
from app.modules.admin import service as admin_service
from app.modules.applications import service
from app.modules.applications.models import Application
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.gis import service as gis_service

STATUS_LABELS: dict[str, dict[str, str]] = {
    "DRAFT": {"uz_latn": "Qoralama", "ru": "Черновик"},
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
KIND_LABELS: dict[str, dict[str, str]] = {
    "new": {"uz_latn": "Yangi", "ru": "Новая"},
    "extension": {"uz_latn": "Uzaytirish", "ru": "Продление"},
}
CHANNEL_LABELS: dict[str, dict[str, str]] = {
    "portal": {"uz_latn": "Portal", "ru": "Портал"},
    "mygov": {"uz_latn": "my.gov.uz", "ru": "my.gov.uz"},
}
TITLE = {"uz_latn": "Arizalar", "ru": "Заявки"}
FILENAME_STEM = "arizalar"


def _label(table: dict[str, dict[str, str]], code: str, lang: xlsx.Lang) -> str:
    """An unknown code renders as itself — never an empty cell that hides it."""
    return table.get(code, {}).get(lang, code)


class Row:
    """One application plus the names the sheet shows. The columns read
    attributes off this, so every resolver runs once per table, not per row."""

    def __init__(
        self,
        app: Application,
        *,
        applicant: str,
        activity_type: str,
        contour: str,
        organization: str,
    ) -> None:
        self.app = app
        self.id = app.id
        self.applicant = applicant
        self.activity_type = activity_type
        self.contour = contour
        self.organization = organization


def _attr(name: str) -> Callable[[Row], xlsx.CellValue]:
    return lambda row: getattr(row.app, name)


def columns(lang: xlsx.Lang) -> list[xlsx.Column[Row]]:
    """The screen's columns first (number, status, contour, period, area,
    SLA), then the fields a reader wants beside them (ruling R4), the id last."""
    return [
        xlsx.Column("number", {"uz_latn": "Raqam", "ru": "Номер"}, _attr("number"), 18),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(STATUS_LABELS, r.app.status, lang),
            26,
        ),
        xlsx.Column(
            "kind",
            {"uz_latn": "Turi", "ru": "Вид"},
            lambda r: _label(KIND_LABELS, r.app.kind, lang),
            12,
        ),
        xlsx.Column(
            "applicant", {"uz_latn": "Ariza beruvchi", "ru": "Заявитель"}, lambda r: r.applicant, 30
        ),
        xlsx.Column(
            "activity_type",
            {"uz_latn": "Faoliyat turi", "ru": "Вид деятельности"},
            lambda r: r.activity_type,
            24,
        ),
        xlsx.Column("contour", {"uz_latn": "Kontur", "ru": "Контур"}, lambda r: r.contour, 14),
        xlsx.Column(
            "organization",
            {"uz_latn": "Oʻrmon xoʻjaligi", "ru": "Лесхоз"},
            lambda r: r.organization,
            30,
        ),
        xlsx.Column(
            "period_from", {"uz_latn": "Davr boshi", "ru": "Период с"}, _attr("period_from"), 12
        ),
        xlsx.Column(
            "period_to", {"uz_latn": "Davr oxiri", "ru": "Период по"}, _attr("period_to"), 12
        ),
        xlsx.Column(
            "requested_area_ha",
            {"uz_latn": "Maydon, ga", "ru": "Площадь, га"},
            _attr("requested_area_ha"),
            12,
        ),
        xlsx.Column("quantity", {"uz_latn": "Miqdor", "ru": "Количество"}, _attr("quantity"), 12),
        xlsx.Column(
            "channel",
            {"uz_latn": "Kanal", "ru": "Канал"},
            lambda r: _label(CHANNEL_LABELS, r.app.channel, lang),
            12,
        ),
        xlsx.Column(
            "submitted_at", {"uz_latn": "Yuborilgan", "ru": "Подана"}, _attr("submitted_at"), 18
        ),
        xlsx.Column(
            "sla_deadline_at",
            {"uz_latn": "SLA muddati", "ru": "Срок SLA"},
            _attr("sla_deadline_at"),
            18,
        ),
        xlsx.Column(
            "decided_at", {"uz_latn": "Qaror sanasi", "ru": "Дата решения"}, _attr("decided_at"), 18
        ),
        xlsx.Column(
            "created_at", {"uz_latn": "Yaratilgan", "ru": "Создана"}, _attr("created_at"), 18
        ),
        xlsx.Column(
            "updated_at", {"uz_latn": "Yangilangan", "ru": "Обновлена"}, _attr("updated_at"), 18
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
) -> tuple[list[Row], int, int]:
    """`(rows, total, cap)`. `PageParams.model_construct` bypasses the model's
    own `page_size <= 100` — the export is the one caller legitimately above
    it, and the cap (ruling R3) is what bounds it instead."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    apps, total = await service.list_applications(
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
    )
    applicants = await auth_service.applicant_names(db, {a.applicant_id for a in apps})
    organizations = await admin_service.organization_names(
        db, {a.assigned_org_id for a in apps if a.assigned_org_id is not None}
    )
    activities: dict[uuid.UUID, dict[str, Any]] = await admin_service.activity_type_names(db)
    contours = await gis_service.contour_numbers_by_ids(
        db, {a.contour_id for a in apps if a.contour_id is not None}
    )
    return (
        [
            Row(
                app,
                applicant=applicants.get(app.applicant_id, ""),
                activity_type=(
                    xlsx.localized(activities.get(app.activity_type_id), lang)
                    if app.activity_type_id is not None
                    else ""
                ),
                contour=contours.get(app.contour_id, "") if app.contour_id is not None else "",
                organization=(
                    xlsx.localized(organizations.get(app.assigned_org_id), lang)
                    if app.assigned_org_id is not None
                    else ""
                ),
            )
            for app in apps
        ],
        total,
        cap,
    )


def render(items: Sequence[Row], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, columns(lang), lang=lang, title=TITLE[lang])
