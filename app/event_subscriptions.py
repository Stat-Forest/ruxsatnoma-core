"""The one place `core.events.subscribe(...)` is called (design/01 rule 4), and
with it every other CROSS-MODULE registration a process needs before it serves.

Both `app.main.create_app()` (the API process — embedded workers included, since
it runs `create_app()` before `lifespan()` ever starts them) and
`app.workers.runner.main()` (the standalone `workers_mode=off` worker process)
call `register_event_subscriptions()` from here, so the list of subscribers
exists in exactly one body no matter which of the two processes is running,
rather than being copied into both entry points and drifting apart (review round
1, finding I2). 3.11a's two provider registrations (`gis.OCCUPANCY_PROVIDERS`,
`norms.LOAD_PROVIDERS`) join them for exactly that reason and no other: put in
`app/main.py` instead, they would be absent from the standalone worker, whose
own jobs read a contour's committed load.

A plain top-level module, not `app/core/`: `app/core/` may never import a domain
module (`app/core/events.py`'s own docstring), and a real `subscribe()` call here
will always need one — `app.modules.payments.service.handle_application_approved`,
say. Mirrors `app/models_registry.py`'s role in this codebase: a small module two
otherwise-unrelated entry points both import purely for its registration side
effect, never for a return value.
"""

from app.core import events
from app.modules.applications import events as application_events
from app.modules.payments import events as payments_events
from app.modules.payments import subscribers as payments_subscribers

# The bus name this file subscribes `permits` to, TAKEN FROM ITS PUBLISHER —
# never retyped.
#
# It was a second literal here until 2026-09-03, justified by "payments and
# permits are both level 4 and cannot import each other" (design/01 rule 3).
# True of those two modules, and irrelevant to THIS file: this is the seam
# module, the one place allowed to know both sides — three lines above it
# already imports `payments.subscribers` and `applications.events` for the same
# purpose. Matching by string is what keeps `permits` from importing `payments`;
# it is not a reason for the SEAM to guess at the string.
#
# What the second literal cost: rename `payments.events.PAYMENT_CONFIRMED` and
# the publish goes out under the new name while this subscription stays on the
# old one — and NOTHING fails, because `core.events.publish` on a name with no
# subscribers is a legal no-op. In production the assigned executor then simply
# never learns that a permit is due, on every single payment, silently.
#
# Re-exported under this module's own name because it is this file's answer to
# "what does the seam listen on", and the tests on both sides now read it from
# here or from `payments.events` — the same object either way.
# `tests/test_code_conventions.py::
# test_the_payment_confirmed_bus_name_is_written_down_exactly_once` keeps it
# that way; `tests/test_cross_module_journey.py` proves the hop still lands.
#
# Flat snake_case, no dot: a dotted `payment.confirmed` is a
# `notification_templates.event_code`, never a bus name (permits/events.py has
# the three-vocabularies table).
PAYMENT_CONFIRMED = payments_events.PAYMENT_CONFIRMED


def register_event_subscriptions() -> None:
    """Every `core.events.subscribe(...)` call in the system lives here and
    nowhere else (design/01 rule 4), so no module imports another for the sake
    of an event. The publishers land with the application flow. 3.10a
    `payments` is the first subscriber (task 2): `APPLICATION_APPROVED` issues
    an invoice, `APPLICATION_CANCELLED` cancels any in-force one. 3.11a
    `permits` is the second (payment_confirmed -> tell the executor a permit is
    due — it NOTIFIES only, ruling 19: issuance is a human act), and it also
    fills the two provider seams below.

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
    from app.modules.oversight import service as oversight_service
    from app.modules.permits import subscribers as permits_subscribers

    events.subscribe(
        application_events.APPLICATION_APPROVED, payments_subscribers.on_application_approved
    )
    events.subscribe(
        application_events.APPLICATION_CANCELLED, payments_subscribers.on_application_cancelled
    )
    subscribe(PAYMENT_CONFIRMED, permits_subscribers.on_payment_confirmed)

    # 4.2 `oversight`: a THIRD subscriber on some of these names, beside the
    # two above — `core.events.subscribe` supports more than one handler per
    # event name (a plain list), and nothing here changes what
    # `payments`/`permits` already do. Turns all five into `oversight_events`
    # rows (design/02 § oversight); no module above is modified to produce
    # this — see `oversight.service.record_event`'s own docstring.
    for name in (
        application_events.APPLICATION_SUBMITTED,
        application_events.APPLICATION_APPROVED,
        application_events.APPLICATION_REJECTED,
        application_events.APPLICATION_CANCELLED,
    ):
        events.subscribe(name, oversight_service.record_event)
    subscribe(PAYMENT_CONFIRMED, oversight_service.record_event)

    _register_providers()


def _register_providers() -> None:
    """The two registered seams `gis` (3.6a) and `norms` (3.7) shipped empty for
    `permits` to fill: how much of a contour's area is taken, and how many
    conditional heads are already committed on it.

    Here rather than in `app/main.py` for the same reason the subscriptions are:
    a `workers_mode=off` deployment runs `app.workers.runner.main()`, which never
    calls `create_app()`, so a registration in `main.py` would leave that process
    answering `occupancy_source: "none"` while the API process answers
    `"permits"` — the same silent split the outbox's sender registry already
    taught this codebase to avoid.

    **Membership-checked, not appended.** `core.events.subscribe` dedups its own
    `(name, handler)` pairs; these two lists are plain module globals with no
    such guard, and `tests/conftest.py`'s autouse `_isolate_subscriptions` calls
    this function for EVERY test while snapshotting only `events._SUBSCRIBERS`.
    A bare `.append()` would therefore add one copy per test, occupancy would
    silently double and then triple, and the failure would read as test pollution
    rather than as a registration bug — passing whenever a file was run alone.
    """
    from app.modules.admin import open_work as admin_open_work
    from app.modules.applications import service as applications_service
    from app.modules.gis import service as gis_service
    from app.modules.norms import service as norms_service
    from app.modules.permits import service as permits_service

    if permits_service.occupancy_provider not in gis_service.OCCUPANCY_PROVIDERS:
        gis_service.OCCUPANCY_PROVIDERS.append(permits_service.occupancy_provider)
    if permits_service.load_provider not in norms_service.LOAD_PROVIDERS:
        norms_service.LOAD_PROVIDERS.append(permits_service.load_provider)

    # `admin` (level 1) may not read `applications` (3), and tz/04 С23 makes
    # deletion/archival conditional on what it knows (findings F4/F5, plan
    # 07.6). Registered here for the reason every provider above it is: put
    # in `main.py`, the standalone worker process would answer "holds
    # nothing" on every check, which is the exact defect this seam exists to
    # close. `inspections` registers its own OPEN_WORK_PROVIDERS entry
    # separately (plan 07.6 track B) — not from this file's stage-7.6 commit,
    # which only ever ran with `applications` on this branch.
    if applications_service.open_work_provider not in admin_open_work.OPEN_WORK_PROVIDERS:
        admin_open_work.OPEN_WORK_PROVIDERS.append(applications_service.open_work_provider)
    if applications_service.open_work_provider_for_org not in admin_open_work.ORG_WORK_PROVIDERS:
        admin_open_work.ORG_WORK_PROVIDERS.append(applications_service.open_work_provider_for_org)
