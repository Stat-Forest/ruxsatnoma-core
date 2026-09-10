"""`GET /notifications/export.xlsx` (stage 13, ruling #204): the caller's own
in-app inbox on paper. `rows()` calls `service.list_inbox` — the same scope
the screen gets (ruling R2) — with the cap as the `page_size`. No id needs
resolving to a name: every row is already the caller's own, and `event_code`
has no label map on `NotificationsPage.tsx` today (`translateNotification`
translates the RENDERED text, not the code) — printed as-is, per the brief."""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.modules.auth.models import User
from app.modules.notifications import service
from app.modules.notifications.models import Notification

# The channel this route ever lists is always "inapp" (this module's own
# docstring: "In-app rows only"), so `channel` is not a column here.
DELIVERY_STATUS_LABELS: dict[str, dict[str, str]] = {
    "queued": {"uz_latn": "Navbatda", "ru": "В очереди"},
    "sent": {"uz_latn": "Yuborilgan", "ru": "Отправлено"},
    "delivered": {"uz_latn": "Yetkazilgan", "ru": "Доставлено"},
    "failed": {"uz_latn": "Yetkazilmadi", "ru": "Не доставлено"},
}
TITLE = {"uz_latn": "Bildirishnomalar", "ru": "Уведомления"}


def _delivery_label(code: str, lang: xlsx.Lang) -> str:
    return DELIVERY_STATUS_LABELS.get(code, {}).get(lang, code)


def _transition_field(params: dict, key: str) -> str:
    """`params.status_from`/`status_to` (`TransitionChips.tsx`'s own two
    fields) as plain text — `""` when the notification carries neither
    (a reminder, a recalculation)."""
    value = params.get(key)
    return str(value) if isinstance(value, str) and value else ""


def columns(lang: xlsx.Lang) -> list[xlsx.Column[Notification]]:
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


async def rows(
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


def render(items: Sequence[Notification], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, columns(lang), lang=lang, title=TITLE[lang])
