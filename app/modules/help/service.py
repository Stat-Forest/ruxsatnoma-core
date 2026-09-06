"""help service — FAQ (plain CRUD) and support tickets (one linear status
machine, ruling R6: `plans/04.6-4.8-public-help.md`). The lowest-priority
module in the whole project plan (`tz/02`), built flat on purpose: no FAQ
versioning, no SLA clock on a ticket.

Ticket bodies are written by any authenticated user, staff or applicant —
never anonymous, but still never echoed into a raised exception or a log line
(the same discipline `public`'s ruling R5 states for its own, more exposed
surface)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import numbers
from app.core.errors import err
from app.core.schemas import PageParams
from app.core.time import business_today
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth.deps import SUPERUSER_ROLE
from app.modules.auth.models import User
from app.modules.help import repo
from app.modules.help.models import (
    TICKET_NUMBER_PREFIX,
    TICKET_TRANSITIONS,
    FaqItem,
    SupportTicket,
    SupportTicketMessage,
)
from app.modules.help.permissions import TICKETS_MANAGE
from app.modules.help.schemas import FaqIn, FaqPatch

# --- FAQ -----------------------------------------------------------------


async def list_public_faq(db: AsyncSession, *, category: str | None = None) -> list[FaqItem]:
    items = await repo.list_faq(db, status="published")
    if category is not None:
        items = [item for item in items if item.category == category]
    return items


async def list_faq_admin(db: AsyncSession, *, status: str | None) -> list[FaqItem]:
    return await repo.list_faq(db, status=status)


async def _faq_or_404(db: AsyncSession, faq_id: uuid.UUID) -> FaqItem:
    item = await repo.get_faq(db, faq_id)
    if item is None:
        raise err("ERR-SYS-003")
    return item


async def create_faq(db: AsyncSession, *, data: FaqIn, actor: User) -> FaqItem:
    item = FaqItem(
        category=data.category,
        question=data.question.root,
        answer=data.answer.root,
        sort_order=data.sort_order,
        status="draft",
    )
    await repo.add(db, item)
    await audit.log(
        db, action="faq.create", user_id=actor.id, object_type="faq_item", object_id=item.id
    )
    return item


async def update_faq(
    db: AsyncSession, faq_id: uuid.UUID, *, data: FaqPatch, actor: User
) -> FaqItem:
    item = await _faq_or_404(db, faq_id)
    changes = data.model_dump(exclude_unset=True)
    if "question" in changes and data.question is not None:
        item.question = data.question.root
    if "answer" in changes and data.answer is not None:
        item.answer = data.answer.root
    for field in ("category", "sort_order", "status"):
        if field in changes:
            setattr(item, field, changes[field])
    await audit.log(
        db, action="faq.update", user_id=actor.id, object_type="faq_item", object_id=item.id
    )
    return item


# --- Support tickets -------------------------------------------------------


async def _may_manage_tickets(db: AsyncSession, actor: User) -> bool:
    if await auth_repo.role_code(db, actor) == SUPERUSER_ROLE:
        return True
    return TICKETS_MANAGE in await auth_repo.permission_codes(db, actor)


async def create_ticket(
    db: AsyncSession, *, subject: str, body: str, file_id: uuid.UUID | None, actor: User
) -> SupportTicket:
    number = await numbers.next_public_number(db, TICKET_NUMBER_PREFIX, business_today())
    ticket = SupportTicket(number=number, user_id=actor.id, subject=subject, status="new")
    await repo.add(db, ticket)
    await repo.add(
        db,
        SupportTicketMessage(ticket_id=ticket.id, author_id=actor.id, body=body, file_id=file_id),
    )
    await audit.log(
        db,
        action="ticket.create",
        user_id=actor.id,
        object_type="support_ticket",
        object_id=ticket.id,
    )
    return ticket


async def list_tickets(
    db: AsyncSession, *, actor: User, status: str | None, params: PageParams
) -> tuple[list[SupportTicket], int]:
    """Every ticket for a `TICKETS_MANAGE` holder; otherwise only tickets the
    caller opened or is assigned to (`repo.list_tickets`'s own `user_id`
    branch)."""
    user_id = None if await _may_manage_tickets(db, actor) else actor.id
    return await repo.list_tickets(
        db, user_id=user_id, status=status, offset=params.offset, limit=params.page_size
    )


async def _ticket_or_404(db: AsyncSession, ticket_id: uuid.UUID) -> SupportTicket:
    ticket = await repo.get_ticket(db, ticket_id)
    if ticket is None:
        raise err("ERR-SYS-003")
    return ticket


async def _assert_visible(db: AsyncSession, ticket: SupportTicket, actor: User) -> None:
    if ticket.user_id == actor.id or ticket.assigned_to == actor.id:
        return
    if await _may_manage_tickets(db, actor):
        return
    raise err("ERR-ACL-001")


async def get_ticket(
    db: AsyncSession, ticket_id: uuid.UUID, *, actor: User
) -> tuple[SupportTicket, list[SupportTicketMessage]]:
    ticket = await _ticket_or_404(db, ticket_id)
    await _assert_visible(db, ticket, actor)
    return ticket, await repo.list_messages(db, ticket_id)


async def add_message(
    db: AsyncSession,
    ticket_id: uuid.UUID,
    *,
    body: str,
    file_id: uuid.UUID | None,
    actor: User,
) -> SupportTicketMessage:
    ticket = await _ticket_or_404(db, ticket_id)
    await _assert_visible(db, ticket, actor)
    if ticket.status == "closed":
        raise err("ERR-HELP-001", details={"status": ticket.status})
    message = SupportTicketMessage(
        ticket_id=ticket.id, author_id=actor.id, body=body, file_id=file_id
    )
    await repo.add(db, message)
    await audit.log(
        db,
        action="ticket.message",
        user_id=actor.id,
        object_type="support_ticket",
        object_id=ticket.id,
    )
    return message


def _assert_transition(ticket: SupportTicket, to_status: str) -> None:
    allowed = TICKET_TRANSITIONS.get(ticket.status, ())
    if to_status not in allowed:
        raise err("ERR-HELP-001", details={"from_status": ticket.status, "to_status": to_status})


async def assign_ticket(
    db: AsyncSession, ticket_id: uuid.UUID, *, assignee_id: uuid.UUID, actor: User
) -> SupportTicket:
    ticket = await _ticket_or_404(db, ticket_id)
    if ticket.status == "new":
        _assert_transition(ticket, "in_progress")
        ticket.status = "in_progress"
    ticket.assigned_to = assignee_id
    await audit.log(
        db,
        action="ticket.assign",
        user_id=actor.id,
        object_type="support_ticket",
        object_id=ticket.id,
        new_value={"assigned_to": str(assignee_id)},
    )
    return ticket


async def resolve_ticket(db: AsyncSession, ticket_id: uuid.UUID, *, actor: User) -> SupportTicket:
    ticket = await _ticket_or_404(db, ticket_id)
    _assert_transition(ticket, "resolved")
    ticket.status = "resolved"
    await audit.log(
        db,
        action="ticket.resolve",
        user_id=actor.id,
        object_type="support_ticket",
        object_id=ticket.id,
    )
    return ticket


async def close_ticket(db: AsyncSession, ticket_id: uuid.UUID, *, actor: User) -> SupportTicket:
    ticket = await _ticket_or_404(db, ticket_id)
    await _assert_visible(db, ticket, actor)
    _assert_transition(ticket, "closed")
    ticket.status = "closed"
    ticket.closed_at = datetime.now(UTC)
    await audit.log(
        db,
        action="ticket.close",
        user_id=actor.id,
        object_type="support_ticket",
        object_id=ticket.id,
    )
    return ticket
