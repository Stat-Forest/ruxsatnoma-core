"""Audit service: the only write path to audit_log for all modules.

Invariant (design/01 rule 6): the trail is written in the SAME transaction
as the action it records — this service never commits; the caller's
transaction (commit-on-success in get_db, decision #37) makes the action
and its trail durable atomically, or rolls back both.
"""

import uuid
from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import CORRELATION_ID_KEY
from app.modules.audit import repo
from app.modules.audit.models import AuditLog


async def log(
    db: AsyncSession,
    *,
    action: str,
    user_id: uuid.UUID | None = None,
    object_type: str | None = None,
    object_id: uuid.UUID | None = None,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    basis: str | None = None,
    result: Literal["success", "denied", "error"] = "success",
    ip: str | None = None,
    user_agent: str | None = None,
    correlation_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one audit entry within the caller's transaction.

    `action` follows "<object>.<verb>" in English ("application.submit",
    "user.block", "export.run"); constants belong to the acting module —
    audit is level 0 and knows no domain vocabularies. `user_id` is None
    for system/worker actions. When `correlation_id` is not given, the
    request id bound by the correlation middleware is picked up from
    structlog contextvars (workers pass their own explicitly).

    A raised exception rolls back the caller's transaction INCLUDING this
    trail — so `result="denied"`/`"error"` entries survive only on
    non-raising paths or via a separate transaction; the denied/error audit
    path is designed in stage 3.2.
    """
    if correlation_id is None:
        correlation_id = structlog.contextvars.get_contextvars().get(CORRELATION_ID_KEY)
    entry = AuditLog(
        action=action,
        user_id=user_id,
        object_type=object_type,
        object_id=object_id,
        old_value=old_value,
        new_value=new_value,
        basis=basis,
        result=result,
        ip=ip,
        user_agent=user_agent,
        correlation_id=correlation_id,
        extra=extra,
    )
    await repo.add(db, entry)
    return entry
