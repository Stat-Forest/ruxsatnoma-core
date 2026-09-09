"""Outbox lifecycle + integration log. Level 0: actor ids, never User objects."""

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.errors import err
from app.core.time import in_quiet_hours
from app.db import uuid7
from app.modules.audit import service as audit
from app.modules.integrations import breaker, repo
from app.modules.integrations.adapters.eimzo import EimzoCall, EimzoError, get_eimzo_adapter
from app.modules.integrations.models import InboundDeadLetter, IntegrationLog, OutboxMessage
from app.modules.integrations.senders import SENDERS, register_sender

logger = structlog.get_logger(__name__)


async def enqueue(
    db: AsyncSession,
    *,
    destination: str,
    payload: dict[str, Any],
    idempotency_key: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> OutboxMessage | None:
    """Insert an outbox row in the CALLER'S transaction (atomic with the action).
    With an idempotency_key, a duplicate insert is silently skipped (returns None)."""
    if idempotency_key is None:
        row = OutboxMessage(destination=destination, payload=payload, correlation_id=correlation_id)
        db.add(row)
        await db.flush()
        return row
    result = await db.execute(
        pg_insert(OutboxMessage)
        .values(
            id=uuid7(),
            destination=destination,
            payload=payload,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(OutboxMessage.id)
    )
    new_id = result.scalar_one_or_none()
    if new_id is None:
        return None
    return await db.get(OutboxMessage, new_id)


# The one destination the quiet window covers (decision #152). `sms_otp` is
# deliberately absent: a verification code is something a person is waiting for on
# the screen in front of them, so holding it until morning would not be politeness
# but an outage. `email` is not covered either — a night-time e-mail wakes nobody.
QUIET_HOURS_DESTINATION = "sms"


async def _paused_destinations(db: AsyncSession) -> list[str]:
    """Destinations that must not be CLAIMED right now: a destination whose breaker
    is open, plus `sms` inside the nightly quiet window.

    Holding the row back at CLAIM time rather than failing it at delivery time is
    what keeps the retry budget intact — a message held for eight hours must arrive
    at 08:00 with all of its attempts left, not one attempt short of `dead` (the
    same reasoning `pick_due` already applies to an open breaker). It also covers a
    retry that lands inside the window although the first attempt did not.
    """
    paused = list(breaker.open_destinations())
    if QUIET_HOURS_DESTINATION in paused:
        return paused
    start = await settings_store.get_int(db, "sms_quiet_hours_start")
    end = await settings_store.get_int(db, "sms_quiet_hours_end")
    if in_quiet_hours(start, end):
        paused.append(QUIET_HOURS_DESTINATION)
    return paused


async def deliver_one(db: AsyncSession) -> bool:
    """Claim and deliver one due message; commits the outcome. False = queue idle.

    The claim is the open transaction itself: a crash rolls back to 'pending'
    and the row is retried — no reaper, no stuck 'delivering' rows (ruling 5)."""
    row = await repo.pick_due(db, exclude_destinations=await _paused_destinations(db))
    if row is None:
        # Belt-and-braces: closes the open read transaction that made clock_timestamp necessary.
        await db.rollback()
        return False
    started = time.monotonic()
    sender = SENDERS.get(row.destination)
    now = datetime.now(UTC)
    if sender is None:
        row.status = "dead"
        row.last_error = f"unknown destination: {row.destination}"
        logger.error(
            "outbox.dead", message_id=str(row.id), destination=row.destination, error=row.last_error
        )
    else:
        try:
            await sender(db, row.payload)
        except Exception as exc:  # noqa: BLE001 — any sender failure is a retry case
            row.attempts += 1
            row.last_error = repr(exc)[:1000]
            # Every trip sits the destination out for outbox_breaker_cooldown_seconds
            # on top of the backoff below, and while open it holds back ALL of that
            # destination's pending rows, not just this one — so time-to-dead is no
            # longer bounded purely by outbox_max_attempts x backoff; the breaker can
            # only push it later, never earlier.
            breaker.record_failure(
                row.destination,
                threshold=await settings_store.get_int(db, "outbox_breaker_failures"),
                cooldown_seconds=await settings_store.get_int(
                    db, "outbox_breaker_cooldown_seconds"
                ),
            )
            max_attempts = await settings_store.get_int(db, "outbox_max_attempts")
            if row.attempts >= max_attempts:
                row.status = "dead"
                logger.error(
                    "outbox.dead",
                    message_id=str(row.id),
                    destination=row.destination,
                    attempts=row.attempts,
                    error=row.last_error,
                )
            else:
                base = await settings_store.get_int(db, "outbox_backoff_base_minutes")
                # Clamp the exponent: unbounded growth can overflow datetime and poison the queue.
                row.next_attempt_at = now + timedelta(minutes=base * 2 ** min(row.attempts - 1, 20))
        else:
            row.status = "delivered"
            row.delivered_at = now
            breaker.record_success(row.destination)
    await log_integration(
        db,
        direction="out",
        system=row.destination,
        endpoint=str(row.id),
        checksum=hashlib.sha256(
            json.dumps(row.payload, sort_keys=True, default=str).encode()
        ).hexdigest(),
        duration_ms=int((time.monotonic() - started) * 1000),
        correlation_id=row.correlation_id,
        meta={"status": row.status, "attempts": row.attempts},
    )
    await db.commit()
    return True


async def log_integration(
    db: AsyncSession,
    *,
    direction: str,
    system: str,
    endpoint: str,
    http_status: int | None = None,
    duration_ms: int | None = None,
    checksum: str | None = None,
    correlation_id: str | None = None,
    idempotency_key: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Append one integration-log row (tz/09 'logging per message').

    Runs in the caller's transaction for now (plan 03.4 ruling 9): a delivery
    attempt and its log entry are committed together by `deliver_one`. Live
    HTTP adapters at stage 5.x will log via a dedicated session instead, so a
    failed delivery still leaves a log entry even if the outer transaction
    rolls back.
    """
    db.add(
        IntegrationLog(
            direction=direction,
            system=system,
            endpoint=endpoint,
            http_status=http_status,
            duration_ms=duration_ms,
            checksum=checksum,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            meta=meta,
        )
    )


async def _log_eimzo_calls(db: AsyncSession, calls: tuple[EimzoCall, ...]) -> None:
    """One `integration_log` row per provider round trip -- mirrors
    `auth.service._log_eimzo_calls`/`signatures.service._log_eimzo_calls`
    (stage 5.2 task 5) for THIS module's own two call sites (Task 7).
    Duplicated rather than shared for the same reason those two mirror each
    other rather than importing one another: each caller owns the session
    the row is written on, and there is no adapter in this codebase that
    opens a transaction of its own. `meta` carries only the provider's own
    numeric status and message (`EimzoCall.provider_status`/
    `.provider_message`) -- never the PKCS#7, never a certificate subject."""
    for call in calls:
        await log_integration(
            db,
            direction="out",
            system="eimzo",
            endpoint=call.endpoint,
            http_status=call.http_status,
            duration_ms=call.duration_ms,
            meta=(
                {"provider_status": call.provider_status, "provider_message": call.provider_message}
                if call.provider_status is not None or call.provider_message is not None
                else None
            ),
        )


def _eimzo_error_details(exc: EimzoError) -> dict[str, Any] | None:
    """Task 7's whole reason to read `EimzoError.provider_status`/`.reason`
    (fix round 1, finding 4, write-only until this route): a refusal must
    reach the caller with the provider's OWN machine-readable reason instead
    of a bare 502/503, so the front end can say "your certificate expired"
    instead of "signature error" (stage 3.8 ruling 9 -- every status code
    keeps its own reason). `None` when the exception carries neither (a
    transport failure never reached the provider at all)."""
    if exc.provider_status is None and exc.reason is None:
        return None
    return {"provider_status": exc.provider_status, "reason": exc.reason}


async def attach_eimzo_timestamp(db: AsyncSession, *, pkcs7: str, ip: str | None) -> str:
    """`POST /api/v1/eimzo/timestamp`'s business logic (Task 7, plan ruling
    R5). A trusted timestamp is mandatory on every signature: without one,
    the only evidence of WHEN a document was signed is the signer's own
    computer clock -- and a permit is a legal document with a validity
    period. Any authenticated caller may reach this; it attaches no meaning
    to the document, only a time.

    Logs the round trip whatever happens (task 5's shape): a refusal is
    logged and committed BEFORE it is re-raised -- nothing else is pending on
    this session at this point, so the commit persists only this one row."""
    adapter = get_eimzo_adapter()
    try:
        stamped = await adapter.attach_timestamp(pkcs7, ip=ip)
    except EimzoError as exc:
        await _log_eimzo_calls(db, getattr(adapter, "calls", ()))
        await db.commit()
        raise err(exc.err_code, details=_eimzo_error_details(exc)) from exc
    await _log_eimzo_calls(db, getattr(adapter, "calls", ()))
    return stamped


async def eimzo_health(db: AsyncSession) -> dict[str, Any]:
    """`GET /api/v1/eimzo/health`'s business logic (Task 7; Task 8's own
    container is what this reads from in `real` mode) -- proxies
    `RealEimzo.health()`'s `/ping` + `/info` so an administrator can see
    whether the VPN is up and when the key expires, without shell access to
    the server. A provider outage raises `EimzoError` like any other call;
    the router lets it surface as its registered `ERR-INT-001`/`ERR-INT-002`
    rather than papering over it as "healthy" -- an administrator checking
    this route needs to see the SAME failure a citizen's signature would
    hit, not a friendlier lie."""
    adapter = get_eimzo_adapter()
    try:
        health = await adapter.health()
    except EimzoError as exc:
        await _log_eimzo_calls(db, getattr(adapter, "calls", ()))
        await db.commit()
        raise err(exc.err_code, details=_eimzo_error_details(exc)) from exc
    await _log_eimzo_calls(db, getattr(adapter, "calls", ()))
    return health


async def requeue_message(
    db: AsyncSession, message_id: uuid.UUID, *, actor_id: uuid.UUID, ip: str | None
) -> OutboxMessage:
    """Manual admin recovery: send a dead row back to the front of the queue.

    Only `dead` rows qualify — a `pending`/`delivered` row is not stuck, so
    requeuing it would be a no-op that misleadingly claims an action happened.
    """
    row = await repo.get_message(db, message_id)
    if row is None:
        raise err("ERR-SYS-003", details={"message": str(message_id)})
    if row.status != "dead":
        raise err("ERR-VAL-001", details={"reason": "not_dead"})
    row.status = "pending"
    row.attempts = 0
    row.next_attempt_at = datetime.now(UTC)
    # `alert_dead_outbox` selects `status='dead' AND alerted_at IS NULL`. Leaving the
    # stamp on would make a SECOND death silent — nobody alerted, and any notification
    # behind it stuck `queued` forever with a dead transport.
    row.alerted_at = None
    await audit.log(
        db,
        action="outbox.requeue",
        user_id=actor_id,
        object_type="outbox_message",
        object_id=row.id,
        ip=ip,
    )
    return row


async def discard_dead_letter(
    db: AsyncSession, letter_id: uuid.UUID, *, actor_id: uuid.UUID, ip: str | None
) -> InboundDeadLetter:
    """Manual admin triage: mark an inbound dead letter as handled/ignored.

    Only `new` letters qualify — one already `discarded`/`reprocessed` has
    already been triaged, so discarding it again would overwrite that trail.
    """
    row = await repo.get_dead_letter(db, letter_id)
    if row is None:
        raise err("ERR-SYS-003", details={"dead_letter": str(letter_id)})
    if row.status != "new":
        raise err("ERR-VAL-001", details={"reason": "not_new"})
    row.status = "discarded"
    row.processed_by = actor_id
    row.processed_at = datetime.now(UTC)
    await audit.log(
        db,
        action="dead_letter.discard",
        user_id=actor_id,
        object_type="inbound_dead_letter",
        object_id=row.id,
        ip=ip,
    )
    return row


# The inbound webhooks that feed the DLQ are ANONYMOUS and the app has no
# body-size middleware, so this column is the one place an unauthenticated caller
# can make us store data of their choosing — and no purge job covers dead letters.
# A few KB is plenty to triage a delivery report by hand.
DEAD_LETTER_PAYLOAD_MAX_BYTES = 4096


def _capped_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Bound what an untrusted body can persist, keeping a triage-sized sample and
    saying plainly that it was cut (`error` beside it is truncated for the same
    reason). `default=str` mirrors what the JSONB bind itself would need."""
    encoded = json.dumps(payload, default=str).encode()
    if len(encoded) <= DEAD_LETTER_PAYLOAD_MAX_BYTES:
        return payload
    return {
        "truncated": True,
        "original_bytes": len(encoded),
        "preview": encoded[:DEAD_LETTER_PAYLOAD_MAX_BYTES].decode(errors="replace"),
    }


async def record_dead_letter(
    db: AsyncSession, *, source: str, payload: dict[str, Any], error: str
) -> InboundDeadLetter:
    """An inbound message we could not interpret (tz/09: schema mismatch → DLQ).
    Written in the caller's transaction; triage happens through /admin/integrations."""
    row = InboundDeadLetter(source=source, payload=_capped_payload(payload), error=error[:1000])
    db.add(row)
    await db.flush()
    logger.warning("dead_letter.recorded", source=source, error=error[:200])
    return row


async def _send_sms_otp(db: AsyncSession, payload: dict[str, Any]) -> None:
    from app.modules.integrations.adapters.otp_sender import get_otp_sender

    await get_otp_sender().send(
        target_type=payload["target_type"], target=payload["target"], code=payload["code"]
    )


register_sender("sms_otp", _send_sms_otp)
