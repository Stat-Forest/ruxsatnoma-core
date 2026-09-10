"""`GET .../export.xlsx` siblings for the admin module's registers (stage 13,
ruling #204): users, organizations, announcements, legal documents, and the
integrations outbox/DLQ.

Each `*_rows()` calls the exact function its own list route calls —
`users_service.list_users`, `repo.list_organizations`,
`announcements_service.list_admin`, `legal_documents_service.list_admin`,
`integrations_repo.list_outbox`/`list_dead_letters` (the last three routes
have no service wrapper of their own either — this file mirrors each list
route exactly rather than inventing one) — with the cap as the page size
(ruling R2: no scope can ever diverge from the screen), then resolves every
id the sheet shows to a name in ONE query per table. Headers and status
labels are copied from the adminka so each file reads like its screen.

The outbox/DLQ `payload` is NEVER exported (lesson: a sender's own
diagnostics must never carry what it was sending — the same reasoning
`OutboxMessageOut`/`DeadLetterOut` already apply to the JSON response). This
file reads the bare ORM rows for those two registers and simply never
touches `.payload` in a column accessor, rather than importing the
router-local `OutboxMessageOut`/`DeadLetterOut` shapes (which would import
`integrations_router` back into this module the routers import FROM).
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.core.schemas import PageParams
from app.modules.admin import (
    announcements_service,
    legal_documents_service,
    repo,
    service,
    users_service,
)
from app.modules.admin.announcements_service import AnnouncementAdminOut
from app.modules.admin.legal_documents_service import LegalDocumentAdminOut
from app.modules.admin.models import Organization
from app.modules.admin.users_schemas import UserAdminOut, UserFilters
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.integrations import repo as integrations_repo
from app.modules.integrations.models import OutboxMessage

_ERROR_MAX = 200


def _label(table: dict[str, dict[str, str]], code: str, lang: xlsx.Lang) -> str:
    """An unknown code renders as itself — never an empty cell that hides it."""
    return table.get(code, {}).get(lang, code)


def _truncate(text: str | None) -> str | None:
    """`last_error`/`error` are admin-visible diagnostics, not the message
    itself — capped the same way the admin API already caps them, so a
    stack-trace-shaped error never turns one row into a wall of text."""
    if text is None:
        return None
    return text[:_ERROR_MAX]


async def _region_names(db: AsyncSession) -> dict[uuid.UUID, dict[str, str]]:
    """The whole catalogue (14 rows) — every export below needs it at most
    once, regardless of how many rows it prints."""
    return {r.id: dict(r.name) for r in await service.list_regions(db)}


async def _district_names(db: AsyncSession) -> dict[uuid.UUID, dict[str, str]]:
    """The whole catalogue (~208 rows) — `repo.list_districts(db, None)` is
    the same call `GET /refs/districts` makes with no `region_id`."""
    return {d.id: dict(d.name) for d in await repo.list_districts(db, None)}


async def _role_names(db: AsyncSession) -> dict[str, dict[str, str]]:
    """Keyed by `code`, not `id`: `UserAdminOut.role_code` is what the row
    carries, the same key `GET /admin/roles` filters by on the screen."""
    return {r.code: dict(r.name) for r in await users_service.list_roles(db)}


# ---------------------------------------------------------------------------
# Users — GET /admin/users/export.xlsx
# ---------------------------------------------------------------------------

USER_STATUS_LABELS: dict[str, dict[str, str]] = {
    "active": {"uz_latn": "Faol", "ru": "Активен"},
    "blocked": {"uz_latn": "Bloklangan", "ru": "Заблокирован"},
    "deleted": {"uz_latn": "Oʻchirilgan", "ru": "Удалён"},
}
USERS_TITLE = {"uz_latn": "Foydalanuvchilar", "ru": "Пользователи"}


class UserRow:
    """One admin user plus the names the sheet shows; the columns read
    attributes off this, so every resolver runs once per table, not per row."""

    def __init__(
        self, user: UserAdminOut, *, role: str, organization: str, region: str, district: str
    ) -> None:
        self.user = user
        self.id = user.id
        self.role = role
        self.organization = organization
        self.region = region
        self.district = district


def user_columns(lang: xlsx.Lang) -> list[xlsx.Column[UserRow]]:
    u = lambda f: lambda r: getattr(r.user, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column("full_name", {"uz_latn": "F.I.Sh.", "ru": "Ф.И.О."}, u("full_name"), 30),
        xlsx.Column("login", {"uz_latn": "Login", "ru": "Логин"}, u("login"), 18),
        xlsx.Column("role", {"uz_latn": "Rol", "ru": "Роль"}, lambda r: r.role, 24),
        xlsx.Column(
            "organization",
            {"uz_latn": "Tashkilot", "ru": "Организация"},
            lambda r: r.organization,
            30,
        ),
        xlsx.Column("region", {"uz_latn": "Hudud", "ru": "Регион"}, lambda r: r.region, 20),
        xlsx.Column("district", {"uz_latn": "Tuman", "ru": "Район"}, lambda r: r.district, 20),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(USER_STATUS_LABELS, r.user.status, lang),
            16,
        ),
        xlsx.Column("phone", {"uz_latn": "Telefon", "ru": "Телефон"}, u("phone"), 16),
        xlsx.Column(
            "email", {"uz_latn": "Elektron pochta", "ru": "Электронная почта"}, u("email"), 26
        ),
        xlsx.Column(
            "last_login_at",
            {"uz_latn": "Oxirgi kirish", "ru": "Последний вход"},
            u("last_login_at"),
            18,
        ),
        xlsx.Column("created_at", {"uz_latn": "Yaratilgan", "ru": "Создан"}, u("created_at"), 18),
        xlsx.id_column(),
    ]


async def users_rows(
    db: AsyncSession, *, actor: User, filters: UserFilters, lang: xlsx.Lang
) -> tuple[list[UserRow], int, int]:
    """(rows, total, cap). `model_construct` bypasses `PageParams`'s own
    `page_size <= 100` — the export is the one caller legitimately above it,
    the cap is what bounds it instead (ruling R3)."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    page = await users_service.list_users(
        db, params=PageParams.model_construct(page=1, page_size=cap), filters=filters, actor=actor
    )
    roles = await _role_names(db)
    orgs = await service.organization_names(
        db, {u.organization_id for u in page.items if u.organization_id}
    )
    regions = await _region_names(db)
    districts = await _district_names(db)
    return (
        [
            UserRow(
                u,
                role=xlsx.localized(roles[u.role_code], lang)
                if u.role_code in roles
                else u.role_code,
                organization=xlsx.localized(orgs.get(u.organization_id), lang)
                if u.organization_id
                else "",
                region=xlsx.localized(regions.get(u.region_id), lang) if u.region_id else "",
                district=xlsx.localized(districts.get(u.district_id), lang)
                if u.district_id
                else "",
            )
            for u in page.items
        ],
        page.total,
        cap,
    )


def render_users(items: Sequence[UserRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, user_columns(lang), lang=lang, title=USERS_TITLE[lang])


# ---------------------------------------------------------------------------
# Organizations — GET /refs/organizations/export.xlsx
# ---------------------------------------------------------------------------

ORG_KIND_LABELS: dict[str, dict[str, str]] = {
    "agency": {"uz_latn": "Agentlik", "ru": "Агентство"},
    "territorial": {"uz_latn": "Hududiy boshqarma", "ru": "Территориальное управление"},
    "leshoz": {"uz_latn": "Oʻrmon xoʻjaligi", "ru": "Лесхоз"},
    "bolim": {"uz_latn": "Boʻlim", "ru": "Отделение"},
    "aylanma": {"uz_latn": "Aylanma", "ru": "Обход"},
    "bolak": {"uz_latn": "Boʻlak", "ru": "Участок"},
}
ORG_STATUS_LABELS: dict[str, dict[str, str]] = {
    "active": {"uz_latn": "Faol", "ru": "Активна"},
    "archived": {"uz_latn": "Arxivlangan", "ru": "В архиве"},
}
ORGANIZATIONS_TITLE = {"uz_latn": "Tashkilotlar", "ru": "Организации"}


class OrganizationRow:
    def __init__(self, org: Organization, *, parent: str, region: str, district: str) -> None:
        self.org = org
        self.id = org.id
        self.parent = parent
        self.region = region
        self.district = district


def organization_columns(lang: xlsx.Lang) -> list[xlsx.Column[OrganizationRow]]:
    o = lambda f: lambda r: getattr(r.org, f)  # noqa: E731
    return [
        xlsx.Column(
            "name",
            {"uz_latn": "Nomi", "ru": "Название"},
            lambda r: xlsx.localized(r.org.name, lang),
            32,
        ),
        xlsx.Column("code", {"uz_latn": "Kod", "ru": "Код"}, o("code"), 16),
        xlsx.Column(
            "kind",
            {"uz_latn": "Turi", "ru": "Тип"},
            lambda r: _label(ORG_KIND_LABELS, r.org.kind, lang),
            22,
        ),
        xlsx.Column(
            "parent", {"uz_latn": "Yuqori tashkilot", "ru": "Вышестоящая"}, lambda r: r.parent, 32
        ),
        xlsx.Column("region", {"uz_latn": "Viloyat", "ru": "Регион"}, lambda r: r.region, 20),
        xlsx.Column("district", {"uz_latn": "Tuman", "ru": "Район"}, lambda r: r.district, 20),
        xlsx.Column("stir", {"uz_latn": "STIR", "ru": "СТИР"}, o("stir"), 14),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(ORG_STATUS_LABELS, r.org.status, lang),
            16,
        ),
        xlsx.id_column(),
    ]


async def organizations_rows(
    db: AsyncSession,
    *,
    lang: xlsx.Lang,
    parent_id: uuid.UUID | None,
    kind: str | None,
    region_id: uuid.UUID | None,
    status: str,
) -> tuple[list[OrganizationRow], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    orgs, total = await repo.list_organizations(
        db, parent_id=parent_id, kind=kind, region_id=region_id, status=status, offset=0, limit=cap
    )
    parents = await service.organization_names(db, {o.parent_id for o in orgs if o.parent_id})
    regions = await _region_names(db)
    districts = await _district_names(db)
    return (
        [
            OrganizationRow(
                o,
                parent=xlsx.localized(parents.get(o.parent_id), lang) if o.parent_id else "",
                region=xlsx.localized(regions.get(o.region_id), lang) if o.region_id else "",
                district=xlsx.localized(districts.get(o.district_id), lang)
                if o.district_id
                else "",
            )
            for o in orgs
        ],
        total,
        cap,
    )


def render_organizations(items: Sequence[OrganizationRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(
        items, organization_columns(lang), lang=lang, title=ORGANIZATIONS_TITLE[lang]
    )


# ---------------------------------------------------------------------------
# Announcements — GET /admin/announcements/export.xlsx
# ---------------------------------------------------------------------------

ANNOUNCEMENT_STATUS_LABELS: dict[str, dict[str, str]] = {
    "draft": {"uz_latn": "Qoralama", "ru": "Черновик"},
    "published": {"uz_latn": "Chop etilgan", "ru": "Опубликовано"},
    "archived": {"uz_latn": "Arxivlangan", "ru": "В архиве"},
}
ANNOUNCEMENTS_TITLE = {"uz_latn": "Eʼlonlar", "ru": "Объявления"}
_AUDIENCE_EVERYONE = {"uz_latn": "Barcha foydalanuvchilar", "ru": "Все пользователи"}


def _audience_summary(
    audience: dict[str, Any] | None,
    roles: dict[str, dict[str, str]],
    regions: dict[uuid.UUID, dict[str, str]],
    lang: xlsx.Lang,
) -> str:
    """Mirrors the adminka's `describeAudience`, condensed to one cell: no
    targeting rule at all reads as "everyone", the same way the screen's
    confirmation dialog does."""
    if not audience:
        return _AUDIENCE_EVERYONE[lang]
    parts: list[str] = []
    role_codes: list[str] = audience.get("role_codes") or []
    if role_codes:
        parts.append(
            ", ".join(
                xlsx.localized(roles[code], lang) if code in roles else str(code)
                for code in role_codes
            )
        )
    region_ids: list[str] = audience.get("region_ids") or []
    if region_ids:
        parts.append(
            ", ".join(xlsx.localized(regions.get(uuid.UUID(str(rid))), lang) for rid in region_ids)
        )
    return "; ".join(parts) if parts else _AUDIENCE_EVERYONE[lang]


class AnnouncementRow:
    def __init__(self, ann: AnnouncementAdminOut, *, audience: str, author: str) -> None:
        self.ann = ann
        self.id = ann.id
        self.audience = audience
        self.author = author


def announcement_columns(lang: xlsx.Lang) -> list[xlsx.Column[AnnouncementRow]]:
    a = lambda f: lambda r: getattr(r.ann, f)  # noqa: E731
    return [
        xlsx.Column(
            "title",
            {"uz_latn": "Sarlavha", "ru": "Заголовок"},
            lambda r: xlsx.localized(r.ann.title, lang),
            34,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(ANNOUNCEMENT_STATUS_LABELS, r.ann.status, lang),
            16,
        ),
        xlsx.Column(
            "audience", {"uz_latn": "Kimga koʻrinadi", "ru": "Кому видно"}, lambda r: r.audience, 30
        ),
        xlsx.Column(
            "public_on_landing", {"uz_latn": "Saytda", "ru": "На сайте"}, a("public_on_landing"), 10
        ),
        xlsx.Column(
            "publish_from",
            {"uz_latn": "Boshlanish sanasi", "ru": "Дата начала"},
            a("publish_from"),
            18,
        ),
        xlsx.Column(
            "publish_to", {"uz_latn": "Tugash sanasi", "ru": "Дата окончания"}, a("publish_to"), 18
        ),
        xlsx.Column("author", {"uz_latn": "Muallif", "ru": "Автор"}, lambda r: r.author, 26),
        xlsx.Column("created_at", {"uz_latn": "Yaratilgan", "ru": "Создано"}, a("created_at"), 18),
        xlsx.id_column(),
    ]


async def announcements_rows(
    db: AsyncSession, *, lang: xlsx.Lang, status: str | None
) -> tuple[list[AnnouncementRow], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    page = await announcements_service.list_admin(
        db, params=PageParams.model_construct(page=1, page_size=cap), status=status
    )
    roles = await _role_names(db)
    regions = await _region_names(db)
    authors = await auth_service.user_names(db, {a.created_by for a in page.items if a.created_by})
    return (
        [
            AnnouncementRow(
                a,
                audience=_audience_summary(a.audience, roles, regions, lang),
                author=authors.get(a.created_by, ""),
            )
            for a in page.items
        ],
        page.total,
        cap,
    )


def render_announcements(items: Sequence[AnnouncementRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(
        items, announcement_columns(lang), lang=lang, title=ANNOUNCEMENTS_TITLE[lang]
    )


# ---------------------------------------------------------------------------
# Legal documents — GET /admin/legal-documents/export.xlsx
# ---------------------------------------------------------------------------

LEGAL_DOCUMENT_STATUS_LABELS: dict[str, dict[str, str]] = {
    "draft": {"uz_latn": "Qoralama", "ru": "Черновик"},
    "published": {"uz_latn": "Chop etilgan", "ru": "Опубликован"},
    "archived": {"uz_latn": "Arxivlangan", "ru": "В архиве"},
}
LEGAL_DOCUMENTS_TITLE = {"uz_latn": "Meʼyoriy-huquqiy hujjatlar", "ru": "Нормативно-правовые акты"}
_SOURCE_FILE = {"uz_latn": "PDF fayl", "ru": "PDF-файл"}
_SOURCE_LINK = {"uz_latn": "lex.uz havolasi", "ru": "Ссылка на lex.uz"}
_SOURCE_NONE = {"uz_latn": "Yoʻq", "ru": "Нет"}


def _source_label(doc: LegalDocumentAdminOut, lang: xlsx.Lang) -> str:
    """Mirrors `LegalDocumentsPage.tsx`'s own precedence: a file wins over a
    bare lex.uz link when a row somehow has both."""
    if doc.file is not None:
        return _SOURCE_FILE[lang]
    if doc.source_url:
        return _SOURCE_LINK[lang]
    return _SOURCE_NONE[lang]


class LegalDocumentRow:
    def __init__(self, doc: LegalDocumentAdminOut, *, source: str, author: str) -> None:
        self.doc = doc
        self.id = doc.id
        self.source = source
        self.author = author


def legal_document_columns(lang: xlsx.Lang) -> list[xlsx.Column[LegalDocumentRow]]:
    d = lambda f: lambda r: getattr(r.doc, f)  # noqa: E731
    return [
        xlsx.Column(
            "doc_number", {"uz_latn": "Hujjat raqami", "ru": "Номер акта"}, d("doc_number"), 20
        ),
        xlsx.Column(
            "title",
            {"uz_latn": "Nomi", "ru": "Название"},
            lambda r: xlsx.localized(r.doc.title, lang),
            34,
        ),
        xlsx.Column(
            "adopted_on", {"uz_latn": "Qabul qilingan", "ru": "Дата принятия"}, d("adopted_on"), 16
        ),
        xlsx.Column("source", {"uz_latn": "Manba", "ru": "Источник"}, lambda r: r.source, 18),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(LEGAL_DOCUMENT_STATUS_LABELS, r.doc.status, lang),
            16,
        ),
        xlsx.Column("author", {"uz_latn": "Muallif", "ru": "Автор"}, lambda r: r.author, 26),
        xlsx.Column("created_at", {"uz_latn": "Yaratilgan", "ru": "Создано"}, d("created_at"), 18),
        xlsx.id_column(),
    ]


async def legal_documents_rows(
    db: AsyncSession, *, lang: xlsx.Lang, status: str | None
) -> tuple[list[LegalDocumentRow], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    page = await legal_documents_service.list_admin(
        db, params=PageParams.model_construct(page=1, page_size=cap), status=status
    )
    authors = await auth_service.user_names(db, {d.created_by for d in page.items if d.created_by})
    return (
        [
            LegalDocumentRow(d, source=_source_label(d, lang), author=authors.get(d.created_by, ""))
            for d in page.items
        ],
        page.total,
        cap,
    )


def render_legal_documents(items: Sequence[LegalDocumentRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(
        items, legal_document_columns(lang), lang=lang, title=LEGAL_DOCUMENTS_TITLE[lang]
    )


# ---------------------------------------------------------------------------
# Integrations outbox — GET /admin/integrations/outbox/export.xlsx
# ---------------------------------------------------------------------------

OUTBOX_STATUS_LABELS: dict[str, dict[str, str]] = {
    "pending": {"uz_latn": "Navbatda", "ru": "В очереди"},
    "delivering": {"uz_latn": "Yuborilmoqda", "ru": "Отправляется"},
    "delivered": {"uz_latn": "Yetkazildi", "ru": "Доставлено"},
    "dead": {"uz_latn": "Yetkazilmadi", "ru": "Не доставлено"},
}
OUTBOX_TITLE = {"uz_latn": "Chiquvchi navbat", "ru": "Очередь отправки"}


def outbox_columns(lang: xlsx.Lang) -> list[xlsx.Column[OutboxMessage]]:
    """Reads the bare `OutboxMessage` row — `.payload` simply never appears
    below (lesson: a sender's own diagnostics must never carry what it was
    sending)."""
    return [
        xlsx.Column(
            "destination",
            {"uz_latn": "Yoʻnalish", "ru": "Направление"},
            lambda r: r.destination,
            22,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(OUTBOX_STATUS_LABELS, r.status, lang),
            16,
        ),
        xlsx.Column(
            "attempts", {"uz_latn": "Urinishlar", "ru": "Попытки"}, lambda r: r.attempts, 12
        ),
        xlsx.Column(
            "created_at", {"uz_latn": "Yaratilgan", "ru": "Создано"}, lambda r: r.created_at, 18
        ),
        xlsx.Column(
            "next_attempt_at",
            {"uz_latn": "Keyingi urinish", "ru": "Следующая попытка"},
            lambda r: r.next_attempt_at,
            18,
        ),
        xlsx.Column(
            "delivered_at",
            {"uz_latn": "Yetkazilgan", "ru": "Доставлено"},
            lambda r: r.delivered_at,
            18,
        ),
        xlsx.Column(
            "last_error",
            {"uz_latn": "Oxirgi xato", "ru": "Последняя ошибка"},
            lambda r: _truncate(r.last_error),
            40,
        ),
        xlsx.id_column(),
    ]


async def outbox_rows(
    db: AsyncSession, *, lang: xlsx.Lang, status: str | None, destination: str | None
) -> tuple[list[OutboxMessage], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    items, total = await integrations_repo.list_outbox(
        db, status=status, destination=destination, page=1, page_size=cap
    )
    return items, total, cap


def render_outbox(items: Sequence[OutboxMessage], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, outbox_columns(lang), lang=lang, title=OUTBOX_TITLE[lang])
