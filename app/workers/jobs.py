"""Periodic jobs (decision #36). Each takes a session factory, opens its own
session, audits with user_id=None, and commits. Failures raise to the caller
(the scheduler wrapper logs and swallows)."""

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import settings_store
from app.core.models import IdempotencyKey
from app.core.time import business_today
from app.modules.audit import service as audit
from app.modules.auth.models import OtpCode, Representation, Session
from app.modules.integrations.models import OutboxMessage

logger = structlog.get_logger(__name__)


async def purge_stale_rows(factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """Delete ephemeral operational rows past their retention (plan 03.4 ruling 11).

    The one sanctioned recurring hard delete: security artifacts, not business
    records — the business trail lives in audit_log/integration_log."""
    counts: dict[str, int] = {}
    async with factory() as db:
        now = datetime.now(UTC)
        otp_days = await settings_store.get_int(db, "purge_otp_after_days")
        sess_days = await settings_store.get_int(db, "purge_sessions_after_days")
        outbox_days = await settings_store.get_int(db, "purge_outbox_delivered_after_days")
        idem_hours = await settings_store.get_int(db, "purge_idempotency_after_hours")

        # .rowcount is a real int at runtime for a Core DELETE executed through
        # AsyncSession (asyncpg's CursorResult) — the async stubs just type
        # execute()'s return as the base Result, which doesn't declare it.
        result = await db.execute(
            delete(OtpCode).where(OtpCode.expires_at < now - timedelta(days=otp_days))
        )
        counts["otp_codes"] = result.rowcount  # pyright: ignore[reportAttributeAccessIssue]
        result = await db.execute(
            delete(Session).where(
                or_(Session.revoked_at.is_not(None), Session.expires_at < now),
                Session.expires_at < now - timedelta(days=sess_days),
            )
        )
        counts["sessions"] = result.rowcount  # pyright: ignore[reportAttributeAccessIssue]
        result = await db.execute(
            delete(OutboxMessage).where(
                OutboxMessage.status == "delivered",
                OutboxMessage.delivered_at < now - timedelta(days=outbox_days),
            )
        )
        counts["outbox_messages"] = result.rowcount  # pyright: ignore[reportAttributeAccessIssue]
        result = await db.execute(
            delete(IdempotencyKey).where(
                IdempotencyKey.created_at < now - timedelta(hours=idem_hours)
            )
        )
        counts["idempotency_keys"] = result.rowcount  # pyright: ignore[reportAttributeAccessIssue]
        await db.commit()
    logger.info("job.purge_stale_rows", **counts)
    return counts


async def expire_representations(factory: async_sessionmaker[AsyncSession]) -> int:
    """Flip past-valid_until active representations to 'expired' (3.2b ruling 14
    carry-over): reads already exclude them; this makes the stored status true."""
    async with factory() as db:
        rows = (
            (
                await db.execute(
                    select(Representation).where(
                        Representation.status == "active",
                        Representation.valid_until.is_not(None),
                        Representation.valid_until < business_today(),
                    )
                )
            )
            .scalars()
            .all()
        )
        correlation = f"job:{uuid.uuid4()}"
        for rep in rows:
            rep.status = "expired"
            await audit.log(
                db,
                action="representation.expire",
                user_id=None,
                object_type="representation",
                object_id=rep.id,
                correlation_id=correlation,
            )
        await db.commit()
    logger.info("job.expire_representations", expired=len(rows))
    return len(rows)
