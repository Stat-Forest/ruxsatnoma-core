"""`GET /admin/public/appeals/export.xlsx` (stage 13, ruling #204): the
staff triage register on paper.

`rows()` calls `service.list_appeals` — the same `public.appeals.manage`
gate and the same `status` filter the screen gets (ruling R2) — with the
cap as the page size. No batch name resolution is needed: a citizen appeal
carries its own `applicant_name`/`contact` inline, there is no id to
resolve against another table.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.core.schemas import PageParams
from app.modules.public import service
from app.modules.public.models import CitizenAppeal

# Copied verbatim from the adminka (`src/i18n/uz_latn.ts` / `ru.ts`,
# `support.appeals.*`) so the file reads like the screen. An unknown status
# renders as itself — never a blank cell that hides which code the row
# actually carried.
STATUS_LABELS: dict[str, dict[str, str]] = {
    "new": {"uz_latn": "Yangi", "ru": "Новое"},
    "in_progress": {"uz_latn": "Jarayonda", "ru": "В работе"},
    "answered": {"uz_latn": "Javob berilgan", "ru": "Отвечено"},
    "closed": {"uz_latn": "Yopilgan", "ru": "Закрыто"},
}
TITLE = {"uz_latn": "Fuqarolar murojaatlari", "ru": "Обращения граждан"}


def _status_label(status: str, lang: xlsx.Lang) -> str:
    return STATUS_LABELS.get(status, {}).get(lang, status)


def _contact(appeal: CitizenAppeal, key: str) -> str:
    value = appeal.contact.get(key) if appeal.contact else None
    return str(value) if value else ""


def columns(lang: xlsx.Lang) -> list[xlsx.Column[CitizenAppeal]]:
    return [
        xlsx.Column("number", {"uz_latn": "Raqami", "ru": "Номер"}, lambda r: r.number, 16),
        xlsx.Column("subject", {"uz_latn": "Mavzu", "ru": "Тема"}, lambda r: r.subject, 30),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _status_label(r.status, lang),
            18,
        ),
        xlsx.Column(
            "applicant_name",
            {"uz_latn": "Murojaat qiluvchi", "ru": "Заявитель"},
            lambda r: r.applicant_name,
            26,
        ),
        xlsx.Column(
            "phone", {"uz_latn": "Telefon", "ru": "Телефон"}, lambda r: _contact(r, "phone"), 18
        ),
        xlsx.Column(
            "email",
            {"uz_latn": "Elektron pochta", "ru": "Эл. почта"},
            lambda r: _contact(r, "email"),
            26,
        ),
        xlsx.Column(
            "created_at",
            {"uz_latn": "Kelib tushgan", "ru": "Поступило"},
            lambda r: r.created_at,
            18,
        ),
        xlsx.Column(
            "answered_at",
            {"uz_latn": "Javob berilgan sana", "ru": "Отвечено"},
            lambda r: r.answered_at,
            18,
        ),
        xlsx.id_column(),
    ]


async def rows(
    db: AsyncSession,
    *,
    lang: xlsx.Lang,
    status: str | None,
) -> tuple[list[CitizenAppeal], int, int]:
    """(rows, total, cap). `model_construct` bypasses `PageParams`'s own
    `page_size <= 100` — the export is the one caller legitimately above it,
    the cap is what bounds it instead (ruling R3)."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    items, total = await service.list_appeals(
        db, status=status, params=PageParams.model_construct(page=1, page_size=cap)
    )
    return list(items), total, cap


def render(items: list[CitizenAppeal], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, columns(lang), lang=lang, title=TITLE[lang])
