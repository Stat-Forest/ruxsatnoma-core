import pytest

from app.core import events
from app.main import _register_event_subscriptions


async def test_a_handler_runs_inside_the_publishers_transaction(db) -> None:
    """Ruling 3а: the handler shares the caller's session, so what it writes
    and what the caller writes commit or roll back as one."""
    seen: list[events.Event] = []

    async def handler(session, event) -> None:
        assert session is db
        seen.append(event)

    events.subscribe("t.one", handler)
    await events.publish(db, events.Event(name="t.one", payload={"id": 7}))

    assert [e.payload["id"] for e in seen] == [7]


async def test_a_raising_handler_propagates_to_the_publisher(db) -> None:
    """The whole point of (а) over (б): an approved application and its invoice
    either both exist or neither does."""

    async def boom(session, event) -> None:
        raise RuntimeError("handler failed")

    events.subscribe("t.two", boom)
    with pytest.raises(RuntimeError, match="handler failed"):
        await events.publish(db, events.Event(name="t.two", payload={}))


async def test_publishing_with_no_subscribers_is_a_no_op(db) -> None:
    """3.9a publishes application_approved before 3.10 exists to hear it."""
    await events.publish(db, events.Event(name="t.nobody", payload={}))


async def test_handlers_run_in_registration_order(db) -> None:
    order: list[str] = []

    async def first(session, event) -> None:
        order.append("first")

    async def second(session, event) -> None:
        order.append("second")

    events.subscribe("t.order", first)
    events.subscribe("t.order", second)
    await events.publish(db, events.Event(name="t.order", payload={}))

    assert order == ["first", "second"]


async def test_subscribing_the_same_handler_twice_does_not_duplicate_it(db) -> None:
    """The mechanism `app.main._register_event_subscriptions` relies on to stay
    safe across repeat calls (ruling C3): `subscribe` refuses a duplicate
    `(event_name, handler)` pair rather than appending a second entry, so a
    handler fires once per event no matter how many times its module's own
    subscribe call runs."""
    calls: list[str] = []

    async def handler(session, event) -> None:
        calls.append(event.name)

    events.subscribe("t.dedup", handler)
    events.subscribe("t.dedup", handler)
    assert events._SUBSCRIBERS["t.dedup"].count(handler) == 1

    await events.publish(db, events.Event(name="t.dedup", payload={}))
    assert calls == ["t.dedup"]


def test_register_event_subscriptions_is_idempotent() -> None:
    """Ruling C3: `create_app()` is called by many tests and by the app itself,
    so `_register_event_subscriptions` must be safe to run more than once per
    process — a subscribe-on-every-call seam would register 3.10a's invoice
    handler N times, and one approved application would raise N invoices.

    3.9a's own seam is empty (no publisher exists yet — see its docstring), so
    this calls the real function twice and asserts the bus's total state is
    unchanged either way; once a later branch adds a real `subscribe(...)`
    call inside it, this same assertion starts exercising that call for free.
    """
    _register_event_subscriptions()
    before = {name: list(handlers) for name, handlers in events._SUBSCRIBERS.items()}

    _register_event_subscriptions()
    after = {name: list(handlers) for name, handlers in events._SUBSCRIBERS.items()}

    assert after == before
