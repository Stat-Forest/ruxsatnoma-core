"""Notifications: template management, rendering, and the notify() entry point
upper modules call inside their own transaction (plan 03.5 ruling 4)."""

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.errors import err
from app.modules.audit import service as audit
from app.modules.auth import service as auth_service
from app.modules.integrations import service as integrations_service
from app.modules.integrations.adapters.email import get_email_sender
from app.modules.integrations.adapters.sms import get_sms_sender
from app.modules.integrations.senders import register_sender
from app.modules.notifications import repo
from app.modules.notifications.models import CHANNELS, Notification, NotificationTemplate
from app.modules.notifications.schemas import TemplateIn

logger = structlog.get_logger(__name__)

# uz_latn, not uz_cyrl: decision #90 makes uz_latn the one language every
# `LocalizedName` (`TemplateIn.body`/`.subject` included) is guaranteed to carry.
FALLBACK_LANGUAGE = "uz_latn"
# The one language every SMS is written in (decision #151). Deliberately the same
# string as the fallback above and deliberately a separate constant: they answer
# different questions — "what do we use when this template has nothing in the
# reader's language" and "what language is an SMS" — and one of them could change
# without the other.
SMS_LANGUAGE = "uz_latn"
# Deliberately narrower than str.format: only {snake_case}. An admin-authored
# template must not be able to reach attributes ({x.__class__}) or indexes,
# and a missing key must not raise inside a business transaction (ruling 9).
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def render(body: dict[str, Any], params: Mapping[str, Any], language: str) -> str:
    text = body.get(language) or body.get(FALLBACK_LANGUAGE) or ""

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            logger.warning("notification.placeholder_missing", placeholder=name)
            return match.group(0)
        return str(params[name])

    return _PLACEHOLDER.sub(_substitute, text)


async def create_template(
    db: AsyncSession, data: TemplateIn, *, actor_id: uuid.UUID, ip: str | None
) -> NotificationTemplate:
    """First version of an (event_code, channel) pair. A pair that already has an
    active version must be superseded instead — otherwise the partial unique index
    would surface as a 500 (the 3.3a lesson: never let IntegrityError be the API)."""
    existing = await repo.get_active_template(db, event_code=data.event_code, channel=data.channel)
    if existing is not None:
        raise err(
            "ERR-VAL-001",
            details={"reason": "active_version_exists", "template_id": str(existing.id)},
        )
    row = NotificationTemplate(
        event_code=data.event_code,
        channel=data.channel,
        subject=data.subject.root if data.subject else None,
        body=data.body.root,
        version=await repo.max_version(db, event_code=data.event_code, channel=data.channel) + 1,
        created_by=actor_id,
    )
    await repo.add(db, row)
    await audit.log(
        db,
        action="notification_template.create",
        user_id=actor_id,
        object_type="notification_template",
        object_id=row.id,
        ip=ip,
        extra={"event_code": row.event_code, "channel": row.channel, "version": row.version},
    )
    return row


async def supersede_template(
    db: AsyncSession,
    template_id: uuid.UUID,
    data: TemplateIn,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> NotificationTemplate:
    """Archive the active version and insert version+1 (ruling 8). The event_code
    and channel may not change — that would be a different template, not a version."""
    old = await repo.get_template(db, template_id)
    if old is None:
        raise err("ERR-SYS-003", details={"template": str(template_id)})
    if old.status != "active":
        raise err("ERR-VAL-001", details={"reason": "already archived"})
    if (data.event_code, data.channel) != (old.event_code, old.channel):
        raise err("ERR-VAL-001", details={"reason": "event_code and channel must match"})
    old.status = "archived"
    await db.flush()  # release the partial unique index before inserting the new active row
    row = NotificationTemplate(
        event_code=old.event_code,
        channel=old.channel,
        subject=data.subject.root if data.subject else None,
        body=data.body.root,
        version=await repo.max_version(db, event_code=old.event_code, channel=old.channel) + 1,
        created_by=actor_id,
    )
    await repo.add(db, row)
    await audit.log(
        db,
        action="notification_template.supersede",
        user_id=actor_id,
        object_type="notification_template",
        object_id=row.id,
        ip=ip,
        extra={"previous_id": str(old.id), "version": row.version},
    )
    return row


async def archive_template(
    db: AsyncSession, template_id: uuid.UUID, *, actor_id: uuid.UUID, ip: str | None
) -> NotificationTemplate:
    """Turn a channel off for an event: notify() then skips sms/email and falls back
    for inapp (ruling 10)."""
    row = await repo.get_template(db, template_id)
    if row is None:
        raise err("ERR-SYS-003", details={"template": str(template_id)})
    if row.status != "active":
        raise err("ERR-VAL-001", details={"reason": "already archived"})
    row.status = "archived"
    await db.flush()
    # `updated_at` only carries onupdate=func.now() (no client-side default), and
    # this is the first module to return that column in the same request that
    # touched it: after the flush above, SQLAlchemy leaves it expired rather than
    # fetching it via RETURNING, so a bare re-read outside the session's async
    # context raises MissingGreenlet — refresh it explicitly while still awaitable.
    await db.refresh(row)
    await audit.log(
        db,
        action="notification_template.archive",
        user_id=actor_id,
        object_type="notification_template",
        object_id=row.id,
        ip=ip,
    )
    return row


DEFAULT_CHANNELS = ("inapp", "sms")


# json.dumps handles exactly these without a custom encoder; the engine configures
# no `json_serializer`, so anything else reaches the JSONB bind and raises.
_JSON_PRIMITIVES = (str, int, float, bool, type(None))


def _jsonable(params: Mapping[str, Any]) -> dict[str, Any]:
    """Make `params` safe to store in the JSONB column.

    A `Decimal` (money is `numeric` by project convention, never float) or a `date`
    — exactly what the seeded `{amount}`, `{due_date}`, `{valid_from}`, `{valid_to}`
    placeholders will be handed at 3.10/3.11 — raises `TypeError: Object of type
    Decimal is not JSON serializable` at flush, INSIDE the caller's business
    transaction. That is the one thing ruling 10 forbids: a content-shaped problem
    must never break the business action (invoice issuance would 500).

    Non-primitives become their `str()` — the same form `render()` already
    substitutes into the text, so display is unchanged; only the stored copy is.
    Containers are stringified too rather than walked: a guaranteed-serializable
    value matters more here than a faithful round-trip of a shape no caller uses.
    """
    return {
        key: value if isinstance(value, _JSON_PRIMITIVES) else str(value)
        for key, value in params.items()
    }


def _fallback_body(event_code: str, params: Mapping[str, Any]) -> str:
    """Ruling 10: an in-app notification is never lost to a missing template. The
    text is deliberately raw — an administrator seeing it knows a template is due."""
    rendered = ", ".join(f"{key}={value}" for key, value in sorted(params.items()))
    return f"{event_code}: {rendered}" if rendered else event_code


SMS_KILL_SWITCH = "notifications_sms_enabled"


class ChannelDisabled(Exception):
    """An operator turned this channel off. Raised — never returned — from the
    delivery path so the outbox's backoff ladder PAUSES the queue: the kill switch
    exists for a spent provider balance, and topping it up must not have cost every
    message queued in the meantime. Carries no personal data (`outbox_messages.
    last_error` is admin-visible and logged) and says plainly that this is an
    operator action, not a provider fault."""


def _recipient_reachable(*, channel: str, contact: auth_service.NotificationContact) -> bool:
    """May this recipient be reached on this channel AT ALL? A False here is
    PERMANENT — no number of retries conjures a verified phone or changes a role, so
    the delivery path fails the notification and returns (the outbox-sender lesson in
    `.claude/lessons.md`). Deliberately separate from the kill switch below, which
    is temporary and has nothing to do with the recipient."""
    if contact.status != "active":
        return False
    if channel == "sms":
        # Decision #150: SMS is for people who are NOT in the system. A staff
        # member reads this same notification in the cabinet they already have
        # open, and every part sent to them is paid for twice over — so the
        # channel is closed to them by ROLE, not by whether they happen to have
        # left their number unverified. This is the only place that decides it:
        # `notify()` callers name a recipient, never a channel.
        if not contact.is_applicant:
            return False
        return bool(contact.phone and contact.phone_verified)
    return bool(contact.email and contact.email_verified)


async def _channel_enabled(db: AsyncSession, channel: str) -> bool:
    """The ops kill switch — an operator's temporary pause. Only `sms` has one
    (rulings 11 and 16)."""
    return channel != "sms" or await settings_store.get_bool(db, SMS_KILL_SWITCH)


async def _transport_allowed(
    db: AsyncSession, *, channel: str, contact: auth_service.NotificationContact
) -> bool:
    """ENQUEUE-time filter only (ruling 11): both reasons mean the same thing here
    — do not queue this message. At delivery time they mean opposite things and
    must be asked separately; see `_deliver`."""
    return _recipient_reachable(channel=channel, contact=contact) and await _channel_enabled(
        db, channel
    )


async def notify(
    db: AsyncSession,
    *,
    event_code: str,
    recipient_user_id: uuid.UUID,
    params: Mapping[str, Any] | None = None,
    channels: Sequence[str] | None = None,
    object_type: str | None = None,
    object_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> list[Notification]:
    """Create the notification rows for one event, in the CALLER'S transaction.

    In-app is always written (С19: legally significant notifications reach the
    cabinet even when other channels are off). Transport channels are enqueued on
    the 3.4 outbox and delivered by its worker — nothing is sent from a request.
    """
    values = _jsonable(params or {})
    requested = tuple(channels) if channels is not None else DEFAULT_CHANNELS
    requested = tuple(dict.fromkeys(requested))  # de-dupe, order preserved: never double-send
    unknown = sorted(set(requested) - set(CHANNELS))
    if unknown:  # a caller typo, not user input — fail loudly in dev and in tests
        raise ValueError(f"unknown notification channels: {unknown}")
    contact = await auth_service.get_notification_contact(db, recipient_user_id)
    if contact is None:
        raise ValueError(f"unknown notification recipient: {recipient_user_id}")

    created: list[Notification] = []
    for channel in ("inapp", *(c for c in requested if c != "inapp")):
        template = await repo.get_active_template(db, event_code=event_code, channel=channel)
        if template is None:
            if channel != "inapp":
                continue  # never send an unrendered SMS
            logger.error("notification.template_missing", event_code=event_code, channel=channel)
        if channel != "inapp" and not await _transport_allowed(
            db, channel=channel, contact=contact
        ):
            continue
        # Decision #151: an SMS is ALWAYS Latin Uzbek, whatever language the
        # recipient reads the cabinet in. Only these texts are submitted for
        # Eskiz moderation, and an unmoderated text does not arrive — so
        # rendering a Russian body here would send a message nothing reports as
        # undelivered. It is also the cheap encoding (GSM 03.38, 160 characters
        # per part against Cyrillic's 70), which is why one language is a real
        # choice rather than a shortcut. `inapp` costs nothing and stays in the
        # recipient's own language.
        language = SMS_LANGUAGE if channel == "sms" else contact.language
        row = Notification(
            recipient_user_id=contact.user_id,
            channel=channel,
            event_code=event_code,
            template_id=template.id if template else None,
            params=values,
            # The language actually RENDERED, not the recipient's preference —
            # `notifications.language` is what the stored text is written in.
            language=language,
            subject=(
                render(template.subject, values, language)
                if template is not None and template.subject
                else None
            ),
            rendered_text=(
                render(template.body, values, language)
                if template is not None
                else _fallback_body(event_code, values)
            ),
            object_type=object_type,
            object_id=object_id,
            correlation_id=correlation_id,
        )
        await repo.add(db, row)
        if channel == "inapp":
            row.status = "delivered"
            row.delivered_at = datetime.now(UTC)
        else:
            message = await integrations_service.enqueue(
                db,
                destination=channel,
                payload={"notification_id": str(row.id)},
                correlation_id=correlation_id,
            )
            assert message is not None  # no idempotency key ⇒ always a fresh row
            row.outbox_message_id = message.id
        created.append(row)
    return created


async def already_notified(
    db: AsyncSession,
    *,
    event_code: str,
    object_id: uuid.UUID,
    channel: str = "inapp",
    recipient_user_id: uuid.UUID | None = None,
    params_match: Mapping[str, str] | None = None,
) -> bool:
    """Whether `object_id` already has a `channel` notification for `event_code`
    — the once-only check a periodic job needs before calling `notify()` again
    for the same object (module boundary: a caller outside `notifications` may
    not query `Notification` directly). `inapp` (the default) is the right
    channel to check: `notify()` always writes it, unlike `sms`/`email`, which
    an unreachable recipient or the kill switch can skip — so it is the one
    channel guaranteed present after a successful call, no new column needed.

    `recipient_user_id` narrows the check to one recipient among several for
    the same object (`applications.jobs.sla_sweep`, which reminds BOTH the
    assigned executor and the organization's head): without it, the first
    recipient's own notification row would make every other recipient look
    already-notified. Omit it (every caller before this one) for the
    original object-wide check.

    `params_match` narrows it FURTHER, to a notification whose stored
    `params` carry these exact key/value pairs too (final whole-branch
    review: `applications.jobs.sla_sweep`'s reminder must re-fire when a
    closed `PENDING_INFO` pause moves `sla_deadline_at`, so its once-only key
    folds the deadline's CURRENT value in — `notify()`'s caller already put it
    in `params`, so no new column is needed to key on it). Optional and
    additive: every caller before this one, and every other caller today,
    omits it and gets the identical query."""
    return await repo.notification_exists(
        db,
        event_code=event_code,
        object_id=object_id,
        channel=channel,
        recipient_user_id=recipient_user_id,
        params_match=params_match,
    )


async def list_inbox(
    db: AsyncSession, *, user_id: uuid.UUID, unread_only: bool, page: int, page_size: int
) -> tuple[list[Notification], int]:
    return await repo.list_inbox(
        db, user_id=user_id, unread_only=unread_only, page=page, page_size=page_size
    )


async def unread_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    return await repo.unread_count(db, user_id)


async def mark_read(
    db: AsyncSession, notification_id: uuid.UUID, *, user_id: uuid.UUID
) -> Notification:
    """Reading someone else's notification must be indistinguishable from reading a
    row that does not exist — a 403 would confirm the id is real."""
    row = await repo.get_notification(db, notification_id)
    if row is None or row.recipient_user_id != user_id or row.channel != "inapp":
        raise err("ERR-SYS-003", details={"notification": str(notification_id)})
    if row.read_at is None:
        row.read_at = datetime.now(UTC)
    return row


async def mark_all_read(db: AsyncSession, user_id: uuid.UUID) -> int:
    return await repo.mark_all_read(db, user_id)


async def _deliver(db: AsyncSession, payload: dict[str, Any]) -> None:
    """Outbox sender for the `sms` and `email` destinations (registered below).

    Runs inside the worker's delivery transaction, so the status write and the
    attempt commit together. Raising means "retry"; returning means "done" — and
    the two conditions `notify()` folds into one enqueue filter mean OPPOSITE
    things here: an unreachable recipient is permanent (fail and return, since no
    retry conjures a verified phone), while the kill switch is an operator's
    temporary pause (raise, so the backoff ladder rides it out).

    `with_for_update` locks the notification row for the whole send: without it, an
    Eskiz delivery report arriving and committing mid-send would set `delivered`
    and then be overwritten by the `sent` write below — the report silently lost
    rather than dead-lettered. The callback now waits instead.
    """
    # populate_existing pairs with EVERY locking `db.get` (final review C2, on
    # `applications.repo`): the lock is taken, but the loader refreshes only
    # unloaded attributes on an instance the session already holds, and
    # `expire_on_commit=False` never expires them — so the lock would be held over
    # a stale copy. Unreachable here today (the outbox opens one session per
    # message, so this row is never already cached) and kept uniform anyway;
    # `tests/test_code_conventions.py` enforces the pairing.
    row = await db.get(
        Notification,
        uuid.UUID(payload["notification_id"]),
        with_for_update=True,
        populate_existing=True,
    )
    if row is None:
        logger.warning("notification.vanished", notification_id=payload["notification_id"])
        return
    contact = await auth_service.get_notification_contact(db, row.recipient_user_id)
    if contact is None or not _recipient_reachable(channel=row.channel, contact=contact):
        row.status = "failed"
        row.error = "recipient is not reachable on this channel"
        return
    if not await _channel_enabled(db, row.channel):
        raise ChannelDisabled(
            f"{row.channel} channel is disabled by an operator "
            f"(system setting {SMS_KILL_SWITCH} is off) — not a provider failure; "
            "the outbox retries until it is switched back on"
        )
    if row.channel == "sms":
        assert contact.phone is not None  # _recipient_reachable guarantees it
        provider_id = await get_sms_sender().send(
            phone=contact.phone, text=row.rendered_text, reference=str(row.id)
        )
    else:
        assert contact.email is not None
        provider_id = await get_email_sender().send(
            to=contact.email, subject=row.subject or "", text=row.rendered_text
        )
    row.status = "sent"
    row.sent_at = datetime.now(UTC)
    row.provider_message_id = provider_id


register_sender("sms", _deliver)
register_sender("email", _deliver)


# Eskiz's report vocabulary (design/04 §4 plus the SMPP statuses it forwards).
DELIVERED_STATUSES = frozenset({"DELIVRD", "DELIVERED"})
FAILED_STATUSES = frozenset(
    {"REJECTD", "REJECTED", "UNDELIV", "UNDELIVERABLE", "FAILED", "EXPIRED", "DELETED"}
)
TERMINAL = frozenset({"delivered", "failed"})


async def apply_delivery_report(db: AsyncSession, data: Mapping[str, Any]) -> str:
    """Apply one provider delivery report. Returns 'ok' | 'ignored' | 'dead_letter'.

    Never raises for bad input: the provider retries a 4xx, and a body we cannot
    interpret will not become interpretable on the third attempt (ruling 18).
    """
    reference = str(data.get("user_sms_id") or "").strip()
    provider_id = str(data.get("message_id") or data.get("id") or "").strip()
    status = str(data.get("status") or "").strip().upper()
    row: Notification | None = None
    if reference:
        try:
            row = await repo.get_notification(db, uuid.UUID(reference))
        except ValueError:
            row = None
    if row is None and provider_id:
        row = await repo.get_by_provider_message_id(db, provider_id)
    result = "ok"
    if row is None or not status:
        await integrations_service.record_dead_letter(
            db,
            source="eskiz",
            payload=dict(data),
            error="unknown notification reference" if status else "missing status",
        )
        result = "dead_letter"
    elif row.status in TERMINAL or status not in DELIVERED_STATUSES | FAILED_STATUSES:
        result = "ignored"
    elif status in DELIVERED_STATUSES:
        row.status = "delivered"
        row.delivered_at = datetime.now(UTC)
    else:
        row.status = "failed"
        row.error = status
    await integrations_service.log_integration(
        db,
        direction="in",
        system="eskiz",
        endpoint="delivery-report",
        meta={"status": status, "result": result},
    )
    return result
