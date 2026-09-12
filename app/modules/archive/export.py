"""`GET /archive/export.xlsx` (stage 13, ruling #204): the archive register
on paper. `rows()` calls `service.list_items` — the same zone the screen
gets (ruling R2) — with the cap as the page size, then resolves every
organization id the sheet shows in ONE batch query (`admin.service.
organization_names`); `ArchivePage.tsx` itself resolves the SAME name
client-side against a separately fetched organization list, since
`ArchiveItemOut` carries only the id."""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.core.schemas import PageParams
from app.modules.admin import service as admin_service
from app.modules.archive import service
from app.modules.archive.schemas import ArchiveItemOut
from app.modules.auth.models import User

OBJECT_TYPE_LABELS: dict[str, dict[str, str]] = {
    "application": {"uz_latn": "Ariza", "ru": "Заявка"},
    "permit": {"uz_latn": "Ruxsatnoma", "ru": "Разрешение"},
}
STATUS_LABELS: dict[str, dict[str, str]] = {
    "stored": {"uz_latn": "Saqlangan", "ru": "Сохранено"},
    "verified": {"uz_latn": "Tekshirilgan", "ru": "Проверено"},
}
TITLE = {"uz_latn": "Arxiv reyestri", "ru": "Архивный реестр"}


def _label(table: dict[str, dict[str, str]], code: str, lang: xlsx.Lang) -> str:
    return table.get(code, {}).get(lang, code)


class Row:
    """One archive item plus the organization name the sheet shows; the
    columns read attributes off this, matching Task A.1's own `Row` idiom."""

    def __init__(self, item: ArchiveItemOut, *, organization: str) -> None:
        self.item = item
        self.id = item.id
        self.organization = organization


def columns(lang: xlsx.Lang) -> list[xlsx.Column[Row]]:
    i = lambda f: lambda r: getattr(r.item, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column(
            "object_type",
            {"uz_latn": "Obyekt turi", "ru": "Тип объекта"},
            lambda r: _label(OBJECT_TYPE_LABELS, r.item.object_type, lang),
            14,
        ),
        xlsx.Column(
            "object_id",
            {"uz_latn": "Obyekt ID", "ru": "ID объекта"},
            lambda r: str(r.item.object_id),
            38,
        ),
        xlsx.Column(
            "organization",
            {"uz_latn": "Tashkilot", "ru": "Организация"},
            lambda r: r.organization,
            30,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holat", "ru": "Статус"},
            lambda r: _label(STATUS_LABELS, r.item.status, lang),
            18,
        ),
        xlsx.Column(
            "archived_at", {"uz_latn": "Arxivlangan", "ru": "Архивировано"}, i("archived_at"), 18
        ),
        xlsx.Column(
            "retention_until",
            {"uz_latn": "Saqlash muddati", "ru": "Хранить до"},
            i("retention_until"),
            14,
        ),
        xlsx.Column(
            "content_hash",
            {"uz_latn": "Kontent xeshi", "ru": "Хеш содержимого"},
            i("content_hash"),
            66,
        ),
        xlsx.Column(
            "storage_ref",
            {"uz_latn": "Saqlash yoʻli", "ru": "Путь в хранилище"},
            i("storage_ref"),
            40,
        ),
        xlsx.id_column(),
    ]


async def rows(
    db: AsyncSession,
    *,
    actor: User,
    lang: xlsx.Lang,
    object_type: str | None,
    status: str | None,
) -> tuple[list[Row], int, int]:
    """(rows, total, cap)."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    page = await service.list_items(
        db,
        actor=actor,
        params=PageParams.model_construct(page=1, page_size=cap),
        object_type=object_type,
        status=status,
    )
    org_ids = {item.organization_id for item in page.items if item.organization_id}
    orgs = await admin_service.organization_names(db, org_ids)
    return (
        [
            Row(
                item,
                organization=xlsx.localized(orgs.get(item.organization_id), lang)
                if item.organization_id
                else "",
            )
            for item in page.items
        ],
        page.total,
        cap,
    )


def render(items: Sequence[Row], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, columns(lang), lang=lang, title=TITLE[lang])
