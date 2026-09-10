"""`GET /beekeepers/export.xlsx` (stage 13, ruling #204): the beekeepers
register on paper. `rows()` calls `service.list_beekeepers` — the same
scope the screen gets (ruling R2, none here: ruling #182's single central
role, no zone) — with the cap as the page size. No cross-module id needs
resolving: `BeekeeperOut` carries no organization/user reference the sheet
would print as a name, only what the register itself owns."""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.core.schemas import PageParams
from app.modules.beekeepers import service
from app.modules.beekeepers.schemas import BeekeeperOut

# Copied from `adminka/src/pages/beekeepers/BeekeepersPage.tsx::STATUS_BADGE_CLASS`
# keys + `src/i18n/*` `beekeepers.status.*`.
STATUS_LABELS: dict[str, dict[str, str]] = {
    "active": {"uz_latn": "Faol", "ru": "Активен"},
    "removed": {"uz_latn": "Chiqarilgan", "ru": "Исключён"},
}
TITLE = {"uz_latn": "Asalarichilar reyestri", "ru": "Реестр пчеловодов"}


def _label(table: dict[str, dict[str, str]], code: str, lang: xlsx.Lang) -> str:
    return table.get(code, {}).get(lang, code)


def columns(lang: xlsx.Lang) -> list[xlsx.Column[BeekeeperOut]]:
    b = lambda f: lambda r: getattr(r, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column(
            "certificate_no",
            {"uz_latn": "Sertifikat raqami", "ru": "Номер сертификата"},
            b("certificate_no"),
            20,
        ),
        xlsx.Column("pinfl", {"uz_latn": "JSHSHIR", "ru": "ПИНФЛ"}, b("pinfl"), 18),
        xlsx.Column("full_name", {"uz_latn": "F.I.Sh.", "ru": "ФИО"}, b("full_name"), 30),
        xlsx.Column(
            "farm_name",
            {"uz_latn": "Xoʻjalik nomi", "ru": "Название хозяйства"},
            b("farm_name"),
            30,
        ),
        # No passport series/number and no STIR: the screen shows neither, and
        # identity documents in a bulk file are a step the screen never takes
        # (ruling R4 adds USEFUL hidden fields, not more personal data). The
        # PINFL stays — it is a screen column, the register's own key beside
        # the certificate number.
        xlsx.Column(
            "status",
            {"uz_latn": "Holat", "ru": "Статус"},
            lambda r: _label(STATUS_LABELS, r.status, lang),
            14,
        ),
        xlsx.Column(
            "removed_reason",
            {"uz_latn": "Chiqarilish sababi", "ru": "Причина исключения"},
            b("removed_reason"),
            30,
        ),
        xlsx.Column(
            "created_at",
            {"uz_latn": "Roʻyxatga olingan", "ru": "Зарегистрирован"},
            b("created_at"),
            18,
        ),
        xlsx.id_column(),
    ]


async def rows(
    db: AsyncSession,
    *,
    q: str | None,
    status: str | None,
) -> tuple[list[BeekeeperOut], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    page = await service.list_beekeepers(
        db, params=PageParams.model_construct(page=1, page_size=cap), q=q, status=status
    )
    return list(page.items), page.total, cap


def render(items: Sequence[BeekeeperOut], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, columns(lang), lang=lang, title=TITLE[lang])
