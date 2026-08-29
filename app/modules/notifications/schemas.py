"""API shapes for notification templates and the in-app inbox."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.schemas import LocalizedName

Channel = Literal["inapp", "sms", "email"]

# An SMS text has to clear Eskiz moderation before it can ever be delivered
# (design/04 §4); every SMS-channel write echoes this back to the admin.
SMS_MODERATION_WARNING = (
    "SMS templates must be registered and approved by the provider before delivery works"
)


class TemplateIn(BaseModel):
    event_code: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=100)
    channel: Channel
    subject: LocalizedName | None = None
    body: LocalizedName


class TemplateOut(BaseModel):
    id: uuid.UUID
    event_code: str
    channel: str
    subject: dict[str, Any] | None
    body: dict[str, Any]
    version: int
    status: str
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    warning: str | None = None


class NotificationOut(BaseModel):
    id: uuid.UUID
    event_code: str
    channel: str
    subject: str | None
    text: str = Field(validation_alias="rendered_text")
    params: dict[str, Any]
    status: str
    object_type: str | None
    object_id: uuid.UUID | None
    created_at: datetime
    read_at: datetime | None

    model_config = {"populate_by_name": True}


class UnreadCountOut(BaseModel):
    count: int


class MarkAllReadOut(BaseModel):
    updated: int
