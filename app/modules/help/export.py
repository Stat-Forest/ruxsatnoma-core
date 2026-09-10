"""`GET /help/tickets/export.xlsx` and `GET /admin/help/faq/export.xlsx`
(stage 13, ruling #204): each list on paper. `ticket_rows()` calls the SAME
`service.list_tickets` the list route calls (ruling R2) — the same scope,
the same filter — then resolves the author/assignee ids to names in ONE
query per table; the ticket BODY is never read here (`help.service`'s own
docstring: never echoed into an export or a log line). `faq_rows()` reads
the whole (unpaged) admin FAQ list and truncates it to the cap in Python —
the underlying route answers a bare `list[FaqOut]`, not a `Page[...]`, so
there is no `page_size` to bound it with going in."""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.core.schemas import PageParams
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.help import service
from app.modules.help.models import SupportTicket

TICKET_STATUS_LABELS: dict[str, dict[str, str]] = {
    "new": {"uz_latn": "Yangi", "ru": "Новое"},
    "in_progress": {"uz_latn": "Jarayonda", "ru": "В работе"},
    "resolved": {"uz_latn": "Hal qilindi", "ru": "Решено"},
    "closed": {"uz_latn": "Yopilgan", "ru": "Закрыто"},
}
TICKETS_TITLE = {"uz_latn": "Yordam soʻrovlari", "ru": "Обращения в поддержку"}


def _label(table: dict[str, dict[str, str]], code: str, lang: xlsx.Lang) -> str:
    """An unrecognised code renders as itself — never a blank cell hiding a
    real, just-unrecognised value."""
    return table.get(code, {}).get(lang, code)


# --- Tickets -----------------------------------------------------------


class TicketRow:
    """One `SupportTicket` plus the names its ids resolve to — columns read
    attributes off this, so `auth_service.user_names` runs once for the
    whole sheet, not once per row."""

    def __init__(self, ticket: SupportTicket, *, author: str, assignee: str) -> None:
        self.ticket = ticket
        self.id = ticket.id
        self.author = author
        self.assignee = assignee


def ticket_columns(lang: xlsx.Lang) -> list[xlsx.Column[TicketRow]]:
    a = lambda f: lambda r: getattr(r.ticket, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column("number", {"uz_latn": "Raqami", "ru": "Номер"}, a("number"), 16),
        xlsx.Column("subject", {"uz_latn": "Mavzu", "ru": "Тема"}, a("subject"), 34),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(TICKET_STATUS_LABELS, r.ticket.status, lang),
            16,
        ),
        xlsx.Column("author", {"uz_latn": "Muallif", "ru": "Автор"}, lambda r: r.author, 26),
        xlsx.Column(
            "assignee", {"uz_latn": "Masʼul xodim", "ru": "Исполнитель"}, lambda r: r.assignee, 26
        ),
        xlsx.Column("created_at", {"uz_latn": "Yaratilgan", "ru": "Создано"}, a("created_at"), 18),
        xlsx.Column("closed_at", {"uz_latn": "Yopilgan", "ru": "Закрыто"}, a("closed_at"), 18),
        xlsx.id_column(),
    ]


async def ticket_rows(
    db: AsyncSession, *, actor: User, lang: xlsx.Lang, status: str | None
) -> tuple[list[TicketRow], int, int]:
    """(rows, total, cap). `model_construct` bypasses `PageParams`'s own
    `page_size <= 100` — the export is the one caller legitimately above it,
    the cap is what bounds it instead (ruling R3)."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    tickets, total = await service.list_tickets(
        db, actor=actor, status=status, params=PageParams.model_construct(page=1, page_size=cap)
    )
    ids = {t.user_id for t in tickets} | {t.assigned_to for t in tickets if t.assigned_to}
    names = await auth_service.user_names(db, ids)
    return (
        [
            TicketRow(
                ticket,
                author=names.get(ticket.user_id, ""),
                assignee=names.get(ticket.assigned_to, "") if ticket.assigned_to else "",
            )
            for ticket in tickets
        ],
        total,
        cap,
    )


def render_tickets(items: Sequence[TicketRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, ticket_columns(lang), lang=lang, title=TICKETS_TITLE[lang])
