"""Two registers of one module on paper (stage 13, ruling #204): the admin's
notification TEMPLATES (`GET /admin/notification-templates/export.xlsx`, the
`columns`/`rows`/`render` group) and the caller's own in-app INBOX
(`GET /notifications/export.xlsx`, the `inbox_*` group).

Templates — `GET /admin/notification-templates/export.xlsx` (stage 13, ruling #204):
the template register on paper — event, channel, version and status, never
the body.

`rows()` calls `repo.list_templates` directly, exactly as
`templates_router.list_templates` does (that route has no service wrapper
of its own — it builds the `Page[...]` inline), with the cap as the page
size (ruling R2: no scope can ever diverge from the screen). Only `subject`
(email only, and short) is exported — never `body`: a template's rendered
text can run to paragraphs and multiple languages, and belongs on the
editor, not squeezed into a spreadsheet cell.
"""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.notifications import repo, service
from app.modules.notifications.models import Notification, NotificationTemplate

CHANNEL_LABELS: dict[str, dict[str, str]] = {
    "inapp": {"uz_latn": "Ilovada", "ru": "В приложении"},
    "sms": {"uz_latn": "SMS", "ru": "SMS"},
    "email": {"uz_latn": "Email", "ru": "Email"},
}
STATUS_LABELS: dict[str, dict[str, str]] = {
    "active": {"uz_latn": "Amaldagi", "ru": "Действующий"},
    "superseded": {"uz_latn": "Eskirgan", "ru": "Устаревший"},
    "archived": {"uz_latn": "Arxivlangan", "ru": "В архиве"},
}
TITLE = {"uz_latn": "Bildirishnoma shablonlari", "ru": "Шаблоны уведомлений"}


def _label(table: dict[str, dict[str, str]], code: str, lang: xlsx.Lang) -> str:
    """An unknown code renders as itself — never an empty cell that hides it."""
    return table.get(code, {}).get(lang, code)


class Row:
    """One template version plus the author's name; the columns read
    attributes off this, so the name resolver runs once per sheet, not per row."""

    def __init__(self, template: NotificationTemplate, *, author: str) -> None:
        self.template = template
        self.id = template.id
        self.author = author


def columns(lang: xlsx.Lang) -> list[xlsx.Column[Row]]:
    t = lambda f: lambda r: getattr(r.template, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column(
            "event_code", {"uz_latn": "Hodisa kodi", "ru": "Код события"}, t("event_code"), 26
        ),
        xlsx.Column(
            "channel",
            {"uz_latn": "Kanal", "ru": "Канал"},
            lambda r: _label(CHANNEL_LABELS, r.template.channel, lang),
            14,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(STATUS_LABELS, r.template.status, lang),
            16,
        ),
        xlsx.Column("version", {"uz_latn": "Versiya", "ru": "Версия"}, t("version"), 10),
        xlsx.Column(
            "subject",
            {"uz_latn": "Mavzu", "ru": "Тема"},
            lambda r: xlsx.localized(r.template.subject, lang),
            30,
        ),
        xlsx.Column("author", {"uz_latn": "Muallif", "ru": "Автор"}, lambda r: r.author, 26),
        xlsx.Column(
            "updated_at", {"uz_latn": "Yangilangan", "ru": "Обновлён"}, t("updated_at"), 18
        ),
        xlsx.id_column(),
    ]


async def rows(
    db: AsyncSession,
    *,
    lang: xlsx.Lang,
    event_code: str | None,
    channel: str | None,
    status: str | None,
) -> tuple[list[Row], int, int]:
    """(rows, total, cap). `list_templates` orders by event code, channel,
    then version descending — the same order the screen's own list reads
    (no `sort` parameter, ruling R6)."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    templates, total = await repo.list_templates(
        db, event_code=event_code, channel=channel, status=status, page=1, page_size=cap
    )
    authors = await auth_service.user_names(db, {t.created_by for t in templates if t.created_by})
    return (
        [Row(t, author=authors.get(t.created_by, "") if t.created_by else "") for t in templates],
        total,
        cap,
    )


def render(items: Sequence[Row], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, columns(lang), lang=lang, title=TITLE[lang])


# --- the caller's own inbox, `GET /notifications/export.xlsx` ---------------
# `inbox_rows()` calls `service.list_inbox` — the same scope the screen gets
# (ruling R2) — with the cap as the `page_size`. No id needs resolving to a
# name: every row is already the caller's own, and `event_code` has no label
# map on `NotificationsPage.tsx` today (`translateNotification` translates the
# RENDERED text, not the code) — printed as-is.
# The channel this route ever lists is always "inapp" (this module's own
# docstring: "In-app rows only"), so `channel` is not a column here.
DELIVERY_STATUS_LABELS: dict[str, dict[str, str]] = {
    "queued": {"uz_latn": "Navbatda", "ru": "В очереди"},
    "sent": {"uz_latn": "Yuborilgan", "ru": "Отправлено"},
    "delivered": {"uz_latn": "Yetkazilgan", "ru": "Доставлено"},
    "failed": {"uz_latn": "Yetkazilmadi", "ru": "Не доставлено"},
}
INBOX_TITLE = {"uz_latn": "Bildirishnomalar", "ru": "Уведомления"}


def _delivery_label(code: str, lang: xlsx.Lang) -> str:
    return DELIVERY_STATUS_LABELS.get(code, {}).get(lang, code)


def _transition_field(params: dict, key: str) -> str:
    """`params.status_from`/`status_to` (`TransitionChips.tsx`'s own two
    fields) as plain text — `""` when the notification carries neither
    (a reminder, a recalculation)."""
    value = params.get(key)
    return str(value) if isinstance(value, str) and value else ""


def inbox_columns(lang: xlsx.Lang) -> list[xlsx.Column[Notification]]:
    n = lambda f: lambda r: getattr(r, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column(
            "event_code", {"uz_latn": "Voqea kodi", "ru": "Код события"}, n("event_code"), 26
        ),
        xlsx.Column("subject", {"uz_latn": "Mavzu", "ru": "Тема"}, n("subject"), 30),
        xlsx.Column("text", {"uz_latn": "Matn", "ru": "Текст"}, n("rendered_text"), 50),
        xlsx.Column(
            "status_from",
            {"uz_latn": "Holat (dan)", "ru": "Статус (из)"},
            lambda r: _transition_field(r.params, "status_from"),
            18,
        ),
        xlsx.Column(
            "status_to",
            {"uz_latn": "Holat (ga)", "ru": "Статус (в)"},
            lambda r: _transition_field(r.params, "status_to"),
            18,
        ),
        xlsx.Column(
            "delivery_status",
            {"uz_latn": "Yetkazish holati", "ru": "Статус доставки"},
            lambda r: _delivery_label(r.status, lang),
            18,
        ),
        xlsx.Column(
            "object_type", {"uz_latn": "Obyekt turi", "ru": "Тип объекта"}, n("object_type"), 16
        ),
        xlsx.Column(
            "object_id",
            {"uz_latn": "Obyekt ID", "ru": "ID объекта"},
            lambda r: str(r.object_id) if r.object_id else "",
            38,
        ),
        xlsx.Column("read_at", {"uz_latn": "Oʻqilgan", "ru": "Прочитано"}, n("read_at"), 18),
        xlsx.Column("created_at", {"uz_latn": "Yaratilgan", "ru": "Создано"}, n("created_at"), 18),
        xlsx.id_column(),
    ]


async def inbox_rows(
    db: AsyncSession,
    *,
    actor: User,
    lang: xlsx.Lang,
    unread: bool,
) -> tuple[list[Notification], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    items, total = await service.list_inbox(
        db, user_id=actor.id, unread_only=unread, page=1, page_size=cap
    )
    return items, total, cap


def inbox_render(items: Sequence[Notification], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, inbox_columns(lang), lang=lang, title=INBOX_TITLE[lang])
