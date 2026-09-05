"""API shapes for `help`."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.schemas import LocalizedName


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
    sort_order: int = 0


class FaqPatch(BaseModel):
    category: str | None = Field(default=None, max_length=100)
    question: LocalizedName | None = None
    answer: LocalizedName | None = None
    sort_order: int | None = None
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
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=5000)
    file_id: uuid.UUID | None = None


class TicketMessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    file_id: uuid.UUID | None = None


class TicketAssignIn(BaseModel):
    assignee_id: uuid.UUID
