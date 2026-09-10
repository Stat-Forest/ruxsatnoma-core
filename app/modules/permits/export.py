"""`GET /permits/export.xlsx` (stage 13, ruling #204): the permits register on
paper.

`rows()` calls `service.list_permits` — the same scope the screen gets
(ruling R2) — with the cap as the page size, then resolves every id the sheet
shows to a name in ONE batch query per table. Column headers and status
labels are copied from the adminka (`src/pages/permits/statusMeta.ts`) so the
file reads like the screen; the number column reuses `service._permit_number`,
the same formatter every notification and the PDF itself print the permit's
number with.
"""

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.core.schemas import PageParams
from app.modules.admin import service as admin_service
from app.modules.applications import service as applications_service
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.permits import service
from app.modules.permits.models import Permit
from app.modules.permits.service import _permit_number

# Copied verbatim from `adminka/src/pages/permits/statusMeta.ts`'s
# `PERMIT_STATUS_LABEL_I18N` (`uz_latn`/`ru`), so the file reads exactly like
# the screen's status badge.
STATUS_LABELS: dict[str, dict[str, str]] = {
    "pending_signatures": {"uz_latn": "Imzolar kutilmoqda", "ru": "Ожидаются подписи"},
    "active": {"uz_latn": "Amalda", "ru": "Действует"},
    "suspended": {"uz_latn": "Toʻxtatilgan", "ru": "Приостановлено"},
    "revoked": {"uz_latn": "Bekor qilingan", "ru": "Аннулировано"},
    "expired": {"uz_latn": "Muddati tugagan", "ru": "Истек срок"},
    "archived": {"uz_latn": "Arxivlangan", "ru": "В архиве"},
}
TITLE = {"uz_latn": "Ruxsatnomalar", "ru": "Разрешения"}


def _status_label(code: str, lang: xlsx.Lang) -> str:
    """An unknown code renders as itself — never an empty cell that hides it."""
    return STATUS_LABELS.get(code, {}).get(lang, code)


class Row:
    """One permit plus the names the sheet shows; the columns read attributes
    off this, so the resolver runs once per table, not once per row."""

    def __init__(
        self,
        permit: Permit,
        *,
        applicant: str,
        activity_type: str,
        organization: str,
        contour: str,
        application: str,
    ) -> None:
        self.permit = permit
        self.id = permit.id
        self.applicant = applicant
        self.activity_type = activity_type
        self.organization = organization
        self.contour = contour
        self.application = application


def columns(lang: xlsx.Lang) -> list[xlsx.Column[Row]]:
    p = lambda f: lambda r: getattr(r.permit, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column(
            "number",
            {"uz_latn": "Ruxsatnoma №", "ru": "№ разрешения"},
            lambda r: _permit_number(r.permit.series, r.permit.number),
            20,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _status_label(r.permit.status, lang),
            22,
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
        xlsx.Column(
            "organization",
            {"uz_latn": "Oʻrmon xoʻjaligi", "ru": "Лесхоз"},
            lambda r: r.organization,
            30,
        ),
        xlsx.Column("contour", {"uz_latn": "Kontur", "ru": "Контур"}, lambda r: r.contour, 14),
        xlsx.Column("area_ha", {"uz_latn": "Maydon, ga", "ru": "Площадь, га"}, p("area_ha"), 12),
        xlsx.Column(
            "period_from", {"uz_latn": "Davr boshi", "ru": "Период с"}, p("period_from"), 12
        ),
        xlsx.Column("period_to", {"uz_latn": "Davr oxiri", "ru": "Период по"}, p("period_to"), 12),
        xlsx.Column("amount", {"uz_latn": "Summa", "ru": "Сумма"}, p("amount"), 16),
        xlsx.Column("sb_load", {"uz_latn": "Yuklama", "ru": "Нагрузка"}, p("sb_load"), 12),
        xlsx.Column(
            "application", {"uz_latn": "Ariza", "ru": "Заявка"}, lambda r: r.application, 18
        ),
        xlsx.Column("issued_at", {"uz_latn": "Berilgan", "ru": "Выдано"}, p("issued_at"), 18),
        xlsx.Column("created_at", {"uz_latn": "Yaratilgan", "ru": "Создано"}, p("created_at"), 18),
        xlsx.id_column(),
    ]


async def rows(
    db: AsyncSession,
    *,
    actor: User,
    lang: xlsx.Lang,
    status: str | None,
    applicant_id: uuid.UUID | None,
    contour_id: uuid.UUID | None,
    organization_id: uuid.UUID | None,
    series: str | None,
    number: int | None,
) -> tuple[list[Row], int, int]:
    """(rows, total, cap). `model_construct` bypasses `PageParams`'s own
    `page_size <= 100` — the export is the one caller legitimately above it,
    and the cap is what bounds it instead (ruling R3)."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    permits, total = await service.list_permits(
        db,
        actor=actor,
        params=PageParams.model_construct(page=1, page_size=cap),
        status=status,
        applicant_id=applicant_id,
        contour_id=contour_id,
        organization_id=organization_id,
        series=series,
        number=number,
    )
    applicants = await auth_service.applicant_names(db, {p.applicant_id for p in permits})
    orgs = await admin_service.organization_names(db, {p.organization_id for p in permits})
    activities = await admin_service.activity_type_names(db)
    contours = await gis_service.contour_numbers_by_ids(db, {p.contour_id for p in permits})
    applications = await applications_service.numbers_by_ids(
        db, {p.application_id for p in permits}
    )
    return (
        [
            Row(
                permit,
                applicant=applicants.get(permit.applicant_id, ""),
                activity_type=xlsx.localized(activities.get(permit.activity_type_id), lang),
                organization=xlsx.localized(orgs.get(permit.organization_id), lang),
                contour=contours.get(permit.contour_id, ""),
                application=applications.get(permit.application_id) or "",
            )
            for permit in permits
        ],
        total,
        cap,
    )


def render(items: Sequence[Row], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, columns(lang), lang=lang, title=TITLE[lang])


# --- the anonymous rating feed, `GET /admin/ratings/export.xlsx` -------------
# Ruling #141 holds in the file as on the screen: date, service, leshoz,
# score, text — never who rated. The id column is the RATING's own id.

RATINGS_TITLE = {"uz_latn": "Baholar", "ru": "Оценки"}
RATINGS_FILENAME_STEM = "baholar"


class RatingRow:
    """One row of `service.list_rating_comments` — a dict with `id`,
    `created_at`, `score`, `comment`, `organization_name`, `activity_type_name`."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.id = raw["id"]


def ratings_columns(lang: xlsx.Lang) -> list[xlsx.Column[RatingRow]]:
    return [
        xlsx.Column(
            "created_at", {"uz_latn": "Sana", "ru": "Дата"}, lambda r: r.raw["created_at"], 18
        ),
        xlsx.Column(
            "activity_type",
            {"uz_latn": "Xizmat", "ru": "Услуга"},
            lambda r: xlsx.localized(r.raw["activity_type_name"], lang),
            24,
        ),
        xlsx.Column(
            "organization",
            {"uz_latn": "Oʻrmon xoʻjaligi", "ru": "Лесхоз"},
            lambda r: xlsx.localized(r.raw["organization_name"], lang),
            30,
        ),
        xlsx.Column("score", {"uz_latn": "Baho", "ru": "Оценка"}, lambda r: r.raw["score"], 8),
        xlsx.Column(
            "comment", {"uz_latn": "Izoh", "ru": "Комментарий"}, lambda r: r.raw["comment"], 60
        ),
        xlsx.id_column(),
    ]


async def ratings_rows(
    db: AsyncSession,
    *,
    actor: User,
    organization_id: uuid.UUID | None,
    activity_type_id: uuid.UUID | None,
    period_from: date,
    period_to: date,
) -> tuple[list[RatingRow], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    raw_rows, total = await service.list_rating_comments(
        db,
        actor=actor,
        params=PageParams.model_construct(page=1, page_size=cap),
        organization_id=organization_id,
        activity_type_id=activity_type_id,
        period_from=period_from,
        period_to=period_to,
    )
    return [RatingRow(r) for r in raw_rows], total, cap


def render_ratings(items: Sequence[RatingRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, ratings_columns(lang), lang=lang, title=RATINGS_TITLE[lang])
