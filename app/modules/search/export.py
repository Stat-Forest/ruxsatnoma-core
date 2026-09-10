"""`GET /search/export.xlsx` (stage 13, ruling #204): the PLAIN register
export beside the prosecutor's watermarked `POST /search/exports` (С22,
ruling #20/#98) — that route and `render.py` stay exactly as they are.

Reuses `service._rows_for`, the SAME function `search()` and `create_export()`
both already go through (this module's own docstring: "the export can never
reach a row the screen's own zone filter would have hidden") — the public
`search()` narrows its result to `SearchResultOut`, which drops the
`organization_name` the repo already joined in for the watermarked export's
own renderer; `_rows_for` is where that column still lives, so this sibling
reads it from there rather than paying a second batch query for a name the
SQL already carries.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.modules.auth.models import User
from app.modules.search import service
from app.modules.search.schemas import SearchKind

KIND_LABELS: dict[str, dict[str, str]] = {
    "applications": {"uz_latn": "Ariza", "ru": "Заявка"},
    "permits": {"uz_latn": "Ruxsatnoma", "ru": "Разрешение"},
}

# Copied verbatim from `adminka/src/pages/staff/format.ts::STATUS_LABELS_I18N`
# (uz_latn/ru only — the two languages this export renders).
APPLICATION_STATUS_LABELS: dict[str, dict[str, str]] = {
    "SUBMITTED": {"uz_latn": "Yuborilgan", "ru": "Отправлено"},
    "IN_REVIEW": {"uz_latn": "Koʻrib chiqilmoqda", "ru": "На рассмотрении"},
    "PENDING_INFO": {"uz_latn": "Maʼlumot kutilmoqda", "ru": "Запрос информации"},
    "RETURNED": {"uz_latn": "Tuzatishga qaytarilgan", "ru": "Возвращено на доработку"},
    "APPROVED": {"uz_latn": "Tasdiqlangan", "ru": "Одобрено"},
    "INVOICED": {"uz_latn": "Hisob-faktura yuborilgan", "ru": "Выставлен счет-фактура"},
    "PAID": {"uz_latn": "Toʻlangan", "ru": "Оплачено"},
    "PERMIT_ISSUED": {"uz_latn": "Ruxsatnoma berilgan", "ru": "Разрешение выдано"},
    "REJECTED": {"uz_latn": "Rad etilgan", "ru": "Отклонено"},
    "CANCELLED": {"uz_latn": "Bekor qilingan", "ru": "Отменено"},
    "EXPIRED_UNPAID": {"uz_latn": "Toʻlanmay muddati oʻtgan", "ru": "Просрочено (не оплачено)"},
    "CLOSED": {"uz_latn": "Yopilgan", "ru": "Закрыто"},
    "ARCHIVED": {"uz_latn": "Arxivlangan", "ru": "В архиве"},
}

# Copied verbatim from `adminka/src/pages/permits/statusMeta.ts::PERMIT_STATUS_LABEL_I18N`.
PERMIT_STATUS_LABELS: dict[str, dict[str, str]] = {
    "pending_signatures": {"uz_latn": "Imzolar kutilmoqda", "ru": "Ожидаются подписи"},
    "active": {"uz_latn": "Amalda", "ru": "Действует"},
    "suspended": {"uz_latn": "Toʻxtatilgan", "ru": "Приостановлено"},
    "revoked": {"uz_latn": "Bekor qilingan", "ru": "Аннулировано"},
    "expired": {"uz_latn": "Muddati tugagan", "ru": "Истек срок"},
    "archived": {"uz_latn": "Arxivlangan", "ru": "В архиве"},
}

TITLE = {"uz_latn": "Qidiruv", "ru": "Поиск"}


def _status_label(kind: str, status: str, lang: xlsx.Lang) -> str:
    """An unknown code renders as itself — never an empty cell that hides it
    (same convention Task A.1's `_label` established)."""
    table = APPLICATION_STATUS_LABELS if kind == "applications" else PERMIT_STATUS_LABELS
    return table.get(status, {}).get(lang, status)


class SearchRow:
    """One search hit plus the `kind` it was fetched under — constant across
    a whole export (`kind` is a required filter, same as `SearchResultOut`'s
    own shape), but still printed per row so the file reads standalone."""

    def __init__(self, row: Row, *, kind: SearchKind) -> None:
        self.row = row
        self.kind = kind
        self.id = row.id


def columns(lang: xlsx.Lang) -> list[xlsx.Column[SearchRow]]:
    return [
        xlsx.Column("number", {"uz_latn": "Raqam", "ru": "Номер"}, lambda r: r.row.number, 20),
        xlsx.Column(
            "kind",
            {"uz_latn": "Turi", "ru": "Вид"},
            lambda r: xlsx.localized(KIND_LABELS[r.kind], lang),
            14,
        ),
        xlsx.Column(
            "applicant",
            {"uz_latn": "Ariza beruvchi", "ru": "Заявитель"},
            lambda r: r.row.applicant_name or "",
            30,
        ),
        xlsx.Column(
            "organization",
            {"uz_latn": "Tashkilot", "ru": "Организация"},
            lambda r: xlsx.localized(r.row.organization_name, lang),
            30,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _status_label(r.kind, r.row.status, lang),
            24,
        ),
        xlsx.Column(
            "created_at", {"uz_latn": "Yaratilgan", "ru": "Создано"}, lambda r: r.row.created_at, 18
        ),
        xlsx.id_column(),
    ]


async def rows(
    db: AsyncSession,
    *,
    actor: User,
    lang: xlsx.Lang,
    kind: SearchKind,
    q: str | None,
    status: str | None,
    organization_id: uuid.UUID | None,
    activity_type_id: uuid.UUID | None,
    series: str | None,
) -> tuple[list[SearchRow], int, int]:
    """(rows, total, cap) — `_rows_for` takes `offset`/`limit` directly
    (ruling R2's other legitimate shape, alongside `PageParams.model_construct`),
    so the cap becomes the limit with no page-size ceiling in the way."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    raw_rows, total = await service._rows_for(  # noqa: SLF001 - this module's own shared seam
        db,
        actor=actor,
        kind=kind,
        q=q,
        status=status,
        organization_id=organization_id,
        activity_type_id=activity_type_id,
        series=series,
        offset=0,
        limit=cap,
    )
    return [SearchRow(row, kind=kind) for row in raw_rows], total, cap


def render(items: Sequence[SearchRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, columns(lang), lang=lang, title=TITLE[lang])
