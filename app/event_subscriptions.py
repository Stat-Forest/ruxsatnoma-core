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


def register_event_subscriptions() -> None:
    """Every `core.events.subscribe(...)` call in the system lives here and
    nowhere else (design/01 rule 4), so no module imports another for the sake
    of an event. Empty in 3.9a: the publishers land with the application flow,
    and the first subscribers are 3.10a `payments` (application_approved ->
    issue an invoice) and 3.11 `permits` (payment_confirmed -> issue a permit).

    Safe to call more than once per process — `subscribe` itself is idempotent
    per `(event_name, handler)` pair (`app/core/events.py`) — because both
    callers need that independently: `create_app()` runs this once per call and
    is itself called by many tests and by the app; the standalone worker
    (`app.workers.runner.main`) calls it again on its own, since it never calls
    `create_app()` at all.
    """
