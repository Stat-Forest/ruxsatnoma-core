"""The synchronous in-transaction event bus (design/01 rule 4, decision #48
ruling 4, plan 03.9a ruling 3а).

A "signal upwards" — level 3 telling level 4 that something happened — is an
event, because the direction of dependency forbids the call. Handlers run
immediately, in the publisher's own session and transaction: if one raises, the
publisher's action rolls back with it. That is deliberate. The flows this bus
carries (approval -> invoice, payment -> status) are exactly the ones that must
not half-happen, and inside one process and one database a transaction is the
strongest consistency tool available.

Subscriptions are registered in `app/main.py` and nowhere else, so no module
imports another for the sake of an event. This module itself imports no domain
module and never will: it takes an `AsyncSession` and plain strings, and does
not know that `applications` (or any other module) exists.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class Event:
    name: str
    payload: Mapping[str, Any]
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


Handler = Callable[[AsyncSession, Event], Awaitable[None]]

_SUBSCRIBERS: dict[str, list[Handler]] = {}


def subscribe(event_name: str, handler: Handler) -> None:
    """Register `handler` to run whenever `event_name` is published, in the
    order handlers are registered (deterministic — a plain list, never a set).

    Idempotent by the `(event_name, handler)` pair: subscribing the exact same
    pair again is a silent no-op rather than a second entry. This is what lets
    `app.main._register_event_subscriptions` — called once per `create_app()`,
    and `create_app()` is called by many tests and by the app itself — run more
    than once per process without a handler firing twice for one event (a bus
    that registered 3.10a's invoice handler N times would raise N invoices for
    one approval). It relies on the handler being a stable, module-level
    function reference, the same idiom the rest of this codebase already uses
    for registration (`auth.permissions.register`, `integrations.senders`) — a
    lambda or closure built fresh on every call would defeat it.

    This differs from `auth.permissions.register`, which *raises* on a repeat
    code: that registry is populated once, as an import-time side effect
    (Python's module cache makes a second run impossible), so a repeat can only
    mean a genuine duplicate code and is a bug worth failing loudly on. A
    subscription seam is called explicitly and repeatedly by design, so the
    same event arriving twice is the expected case, not a bug — it must be
    absorbed quietly, not raised.
    """
    handlers = _SUBSCRIBERS.setdefault(event_name, [])
    if handler not in handlers:
        handlers.append(handler)


async def publish(db: AsyncSession, event: Event) -> None:
    """Run every subscriber of `event.name`, in registration order, inside the
    caller's transaction. No subscribers is a no-op: a publisher must not know
    or care whether a later stage is listening yet."""
    for handler in _SUBSCRIBERS.get(event.name, ()):
        await handler(db, event)
