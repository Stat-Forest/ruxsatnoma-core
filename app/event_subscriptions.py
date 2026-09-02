"""The one place `core.events.subscribe(...)` is called (design/01 rule 4).

Both `app.main.create_app()` (the API process — embedded workers included, since
it runs `create_app()` before `lifespan()` ever starts them) and
`app.workers.runner.main()` (the standalone `workers_mode=off` worker process)
call `register_event_subscriptions()` from here, so the list of subscribers
exists in exactly one body no matter which of the two processes is running,
rather than being copied into both entry points and drifting apart (review round
1, finding I2).

A plain top-level module, not `app/core/`: `app/core/` may never import a domain
module (`app/core/events.py`'s own docstring), and a real `subscribe()` call here
will always need one — `app.modules.payments.service.handle_application_approved`,
say. Mirrors `app/models_registry.py`'s role in this codebase: a small module two
otherwise-unrelated entry points both import purely for its registration side
effect, never for a return value.
"""

# The bus names this file subscribes to, as literal strings. `payments` (3.10a)
# will declare `PAYMENT_CONFIRMED = "payment_confirmed"` in its own
# `events.py` — and importing it from there is exactly what may not happen:
# `payments` and `permits` are both level 4 and neither may import the other
# (design/01 rule 3). Matching by string is the whole reason the bus exists.
# Flat snake_case, no dot: a dotted `payment.confirmed` is a
# `notification_templates.event_code`, never a bus name (permits/events.py has
# the three-vocabularies table).
PAYMENT_CONFIRMED = "payment_confirmed"


def register_event_subscriptions() -> None:
    """Every `core.events.subscribe(...)` call in the system lives here and
    nowhere else (design/01 rule 4), so no module imports another for the sake
    of an event. The publishers land with the application flow; the subscribers
    are 3.10a `payments` (application_approved -> issue an invoice) and 3.11
    `permits` (payment_confirmed -> tell the executor a permit is due — it
    NOTIFIES only, ruling 19: issuance is a human act).

    Safe to call more than once per process — `subscribe` itself is idempotent
    per `(event_name, handler)` pair (`app/core/events.py`) — because both
    callers need that independently: `create_app()` runs this once per call and
    is itself called by many tests and by the app; the standalone worker
    (`app.workers.runner.main`) calls it again on its own, since it never calls
    `create_app()` at all. That second caller is why registration may NOT live
    in `app/main.py`: with `workers_mode=off` the worker process would then run
    with no subscribers at all.
    """
    from app.core.events import subscribe
    from app.modules.permits import subscribers as permits_subscribers

    subscribe(PAYMENT_CONFIRMED, permits_subscribers.on_payment_confirmed)
