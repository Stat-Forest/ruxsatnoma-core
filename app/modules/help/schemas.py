"""API shapes for `help`."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.schemas import SORT_ORDER_MAX, LocalizedName, LongTextStr, NameStr


class FaqOut(BaseModel):
    id: uuid.UUID
    category: str | None
    question: LocalizedName
    answer: LocalizedName
    sort_order: int
    status: str


class FaqIn(BaseModel):
    category: str | None = Field(default=None, max_length=100)
    question: LocalizedName
    answer: LocalizedName
    sort_order: int = Field(default=0, ge=0, le=SORT_ORDER_MAX)


class FaqPatch(BaseModel):
    category: str | None = Field(default=None, max_length=100)
    question: LocalizedName | None = None
    answer: LocalizedName | None = None
    sort_order: int | None = Field(default=None, ge=0, le=SORT_ORDER_MAX)
    status: str | None = Field(default=None, pattern="^(draft|published|archived)$")


class TicketMessageOut(BaseModel):
    id: uuid.UUID
    ticket_id: uuid.UUID
    author_id: uuid.UUID
    body: str
    file_id: uuid.UUID | None
    created_at: datetime


class TicketOut(BaseModel):
    id: uuid.UUID
    number: str
    user_id: uuid.UUID
    subject: str
    status: str
    assigned_to: uuid.UUID | None
    created_at: datetime
    closed_at: datetime | None


class TicketWithMessagesOut(TicketOut):
    messages: list[TicketMessageOut]


class TicketIn(BaseModel):
    # `NameStr`'s 255 matches the adminka's own `TicketFormModal.tsx` maxLength
    # exactly; `LongTextStr`'s 10 000 widens the adminka's 5 000 (C2, final
    # review — never bound below an existing adminka maxLength).
    subject: NameStr
    body: LongTextStr
    file_id: uuid.UUID | None = None


class TicketMessageIn(BaseModel):
    # `LongTextStr` widens the adminka's own 5 000 (`TicketDetailPanel.tsx`).
    body: LongTextStr
    file_id: uuid.UUID | None = None


class TicketAssignIn(BaseModel):
    assignee_id: uuid.UUID
