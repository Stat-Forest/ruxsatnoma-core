"""`GET /reports/export.xlsx` and `GET /reports/forms/export.xlsx` (stage 13,
ruling #204): the reports register and the form catalog on paper. Each
`rows()` calls the list's own service function — `service.list_reports` /
`service.list_forms` — with the cap as the page size (ruling R2), then
resolves every id the sheet shows to a name in ONE batch query per table.
`GET /reports/{report_id}/export.xlsx` (the per-report DATA export,
`service.export_excel`) is untouched."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.core.schemas import PageParams
from app.modules.admin import service as admin_service
from app.modules.auth.models import User
from app.modules.reports import service
from app.modules.reports.models import Report, ReportForm

# Copied verbatim from `adminka/src/pages/reports/transitions.ts::REPORT_STATUSES`
# and `src/i18n/uz_latn.ts` / `ru.ts` (`reports.detail.status.*`).
REPORT_STATUS_LABELS: dict[str, dict[str, str]] = {
    "created": {"uz_latn": "Toʻldirilmoqda", "ru": "Заполняется"},
    "submitted": {"uz_latn": "Rahbar imzosini kutmoqda", "ru": "На подписи у руководителя"},
    "head_approved": {"uz_latn": "Imzolangan, qabulni kutmoqda", "ru": "Подписан, ожидает приёма"},
    "returned": {"uz_latn": "Qayta ishlashga qaytarilgan", "ru": "Возвращён на доработку"},
    "approved": {"uz_latn": "Qabul qilingan", "ru": "Принят"},
}
# Copied from `src/pages/reports/ReportFormsTab.tsx::statusLabelKey` +
# `src/i18n/*` `reports.forms.status.*`.
FORM_STATUS_LABELS: dict[str, dict[str, str]] = {
    "draft": {"uz_latn": "Qoralama", "ru": "Черновик"},
    "active": {"uz_latn": "Faol", "ru": "Активна"},
    "archived": {"uz_latn": "Arxivlangan", "ru": "В архиве"},
}
# `src/pages/reports/ReportFormsTab.tsx::periodTypeLabelKey` + `reports.forms.periodType.*`.
PERIOD_TYPE_LABELS: dict[str, dict[str, str]] = {
    "month": {"uz_latn": "Oy", "ru": "Месяц"},
    "quarter": {"uz_latn": "Chorak", "ru": "Квартал"},
    "year": {"uz_latn": "Yil", "ru": "Год"},
}
REPORTS_TITLE = {"uz_latn": "Hisobotlar", "ru": "Отчёты"}
FORMS_TITLE = {"uz_latn": "Hisobot shakllari", "ru": "Формы отчётов"}


def _label(table: dict[str, dict[str, str]], code: str, lang: xlsx.Lang) -> str:
    return table.get(code, {}).get(lang, code)


async def _form_names_by_ids(
    db: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """The report list's own batch reader for its `form_id` column — this
    module's own table, so a private query here rather than a service call
    to itself (`{}` for an empty set, one query otherwise)."""
    if not ids:
        return {}
    result = await db.execute(select(ReportForm.id, ReportForm.name).where(ReportForm.id.in_(ids)))
    return {row.id: dict(row.name) for row in result}


# --- reports ---------------------------------------------------------------


class ReportRow:
    def __init__(self, report: Report, *, form_name: str, organization: str) -> None:
        self.report = report
        self.id = report.id
        self.form_name = form_name
        self.organization = organization


def report_columns(lang: xlsx.Lang) -> list[xlsx.Column[ReportRow]]:
    r = lambda f: lambda row: getattr(row.report, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column("form", {"uz_latn": "Shakl", "ru": "Форма"}, lambda row: row.form_name, 28),
        xlsx.Column(
            "organization",
            {"uz_latn": "Oʻrmon xoʻjaligi", "ru": "Лесхоз"},
            lambda row: row.organization,
            30,
        ),
        xlsx.Column(
            "period_start", {"uz_latn": "Davr boshi", "ru": "Период с"}, r("period_start"), 14
        ),
        xlsx.Column(
            "period_end", {"uz_latn": "Davr oxiri", "ru": "Период по"}, r("period_end"), 14
        ),
        xlsx.Column("version_no", {"uz_latn": "Versiya", "ru": "Версия"}, r("version_no"), 10),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda row: _label(REPORT_STATUS_LABELS, row.report.status, lang),
            26,
        ),
        xlsx.Column(
            "submitted_at", {"uz_latn": "Yuborilgan", "ru": "Отправлен"}, r("submitted_at"), 18
        ),
        xlsx.Column(
            "approved_at", {"uz_latn": "Qabul qilingan", "ru": "Принят"}, r("approved_at"), 18
        ),
        xlsx.Column(
            "updated_at", {"uz_latn": "Yangilangan", "ru": "Обновлён"}, r("updated_at"), 18
        ),
        xlsx.id_column(),
    ]


async def report_rows(
    db: AsyncSession,
    *,
    actor: User,
    lang: xlsx.Lang,
    organization_id: uuid.UUID | None,
    status: str | None,
    form_id: uuid.UUID | None,
) -> tuple[list[ReportRow], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    reports, total = await service.list_reports(
        db,
        actor=actor,
        organization_id=organization_id,
        status=status,
        form_id=form_id,
        params=PageParams.model_construct(page=1, page_size=cap),
    )
    forms = await _form_names_by_ids(db, {r.form_id for r in reports})
    orgs = await admin_service.organization_names(db, {r.organization_id for r in reports})
    return (
        [
            ReportRow(
                r,
                form_name=xlsx.localized(forms.get(r.form_id), lang),
                organization=xlsx.localized(orgs.get(r.organization_id), lang),
            )
            for r in reports
        ],
        total,
        cap,
    )


def render_reports(items: Sequence[ReportRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, report_columns(lang), lang=lang, title=REPORTS_TITLE[lang])


# --- report_forms ------------------------------------------------------------


class FormRow:
    def __init__(self, form: ReportForm, *, activity_type: str) -> None:
        self.form = form
        self.id = form.id
        self.activity_type = activity_type


def form_columns(lang: xlsx.Lang) -> list[xlsx.Column[FormRow]]:
    f = lambda field: lambda row: getattr(row.form, field)  # noqa: E731
    return [
        xlsx.Column("code", {"uz_latn": "Kod", "ru": "Код"}, f("code"), 20),
        xlsx.Column("version", {"uz_latn": "Versiya", "ru": "Версия"}, f("version"), 10),
        xlsx.Column(
            "name",
            {"uz_latn": "Nomi", "ru": "Название"},
            lambda row: xlsx.localized(row.form.name, lang),
            30,
        ),
        xlsx.Column(
            "period_type",
            {"uz_latn": "Davriyligi", "ru": "Периодичность"},
            lambda row: _label(PERIOD_TYPE_LABELS, row.form.period_type, lang),
            16,
        ),
        xlsx.Column(
            "activity_type",
            {"uz_latn": "Faoliyat turi", "ru": "Вид деятельности"},
            lambda row: row.activity_type,
            24,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda row: _label(FORM_STATUS_LABELS, row.form.status, lang),
            16,
        ),
        xlsx.Column(
            "valid_from", {"uz_latn": "Amal qiladi", "ru": "Действует с"}, f("valid_from"), 14
        ),
        xlsx.id_column(),
    ]


async def form_rows(
    db: AsyncSession,
    *,
    lang: xlsx.Lang,
    status: str | None,
) -> tuple[list[FormRow], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    forms, total = await service.list_forms(
        db, status=status, params=PageParams.model_construct(page=1, page_size=cap)
    )
    activities = await admin_service.activity_type_names(db)
    return (
        [
            FormRow(
                form,
                activity_type=(
                    xlsx.localized(activities.get(form.activity_type_id), lang)
                    if form.activity_type_id
                    else ""
                ),
            )
            for form in forms
        ],
        total,
        cap,
    )


def render_forms(items: Sequence[FormRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, form_columns(lang), lang=lang, title=FORMS_TITLE[lang])
