"""The event codes this module notifies a user on (plan 03.11a ruling 17).

**These are `notification_templates.event_code` values — DOTTED — not bus names.**
Three vocabularies share the word "permit" in this codebase and none of them is
interchangeable with the others; `applications/events.py` carries the full table, and
the underscore/dot split is the tell:

  - **`notification_templates.event_code` (HERE).** Dotted, past participle after the
    dot: `permit.issued`. What `notifications.service.notify(..., event_code=...)`
    looks a template up by, and what migrations seed rows under.
  - **Bus event names** (`app.core.events`). Flat snake_case, no dot:
    `payment_confirmed`, which this module SUBSCRIBES to (ruling 19) and which is
    matched by string in `app/event_subscriptions.py` — `permits` and `payments` are
    both level 4 and may not import each other.
  - **`audit_log.action`.** Dotted, present-tense verb after the dot:
    `permit.issue`. Those constants live with the service that logs them.

Why the list exists at all: with no template, `notify()` writes a raw fallback string
in-app and **sends nothing at all** by SMS or e-mail, silently, with one log line —
while a test asserting "a notification row exists" still passes (ruling 17). So the
set below is asserted against the seeded templates by a test, and anything added here
must be seeded in the same commit.

**Four of the six are seeded by migration 0019 and `permit.due` by 0020;
`permit.issued` was already seeded by `0009_notifications.py`** (both `inapp` and
`sms`, version 1, active) back when notifications shipped.
`uq_notification_templates_active` is a partial unique index on
`(event_code, channel) WHERE status='active'`, so re-seeding it in 0019 would not
"also work" — it would fail the migration outright. A later text change to that row is
a supersede (archive + version 2), never a second active insert."""

PERMIT_ISSUED = "permit.issued"
PERMIT_SIGNED = "permit.signed"
PERMIT_ACTIVE = "permit.active"
# Seeded by 0019, listed below, and SENT BY NOTHING in 3.11a: the reminder job that
# would fire it ("your permit runs out in N days") is a later stage's. Listed anyway,
# because this tuple is what a template must exist FOR, not what fired last night —
# dropping the unsent one would let that job ship with no text at all, which `notify()`
# answers with a raw fallback string in-app and silence by SMS (ruling 17). Pinned by
# `test_end_to_end.py::test_the_notified_set_is_exactly_what_this_module_can_send`.
PERMIT_EXPIRING = "permit.expiring"
PERMIT_EXPIRED = "permit.expired"

# What `subscribers.on_payment_confirmed` sends (ruling 19): money has arrived and
# the assigned hodim must now form the permit. Its own code, seeded by migration
# 0020, rather than `0009`'s `payment.confirmed` — that text is a statement of fact
# addressed to the PAYER, it neither names what an executor must do nor is true of
# them, and 3.10a will send it to the applicant from the very same event. A template
# that exists and says the wrong thing is the failure ruling 17 exists to prevent,
# with a green test on top.
#
# Not to be confused with the BUS name `payment_confirmed` that triggers it
# (`app/event_subscriptions.py`): flat, no dot. The dot is the tell, as above.
PERMIT_DUE = "permit.due"

# --- 3.11b (plan `03.11b-permits-lifecycle` ruling 18): suspend / resume / revoke /
# duplicate / forest ticket / stalled signature. All six are new with this stage and
# all six are seeded by migration 0023, `inapp` and `sms`, active. Five go to the
# permit's HOLDER; `permit.unsigned_stalled` goes to the application's ASSIGNED
# EXECUTOR instead — ruling 16's notify-only sweep for a permit whose period ran
# out before anyone signed it.
PERMIT_SUSPENDED = "permit.suspended"
PERMIT_RESUMED = "permit.resumed"
PERMIT_REVOKED = "permit.revoked"
PERMIT_DUPLICATE_ISSUED = "permit.duplicate_issued"
FOREST_TICKET_ISSUED = "forest_ticket.issued"
PERMIT_UNSIGNED_STALLED = "permit.unsigned_stalled"

NOTIFIED_EVENT_CODES: tuple[str, ...] = (
    PERMIT_ISSUED,
    PERMIT_SIGNED,
    PERMIT_ACTIVE,
    PERMIT_EXPIRING,
    PERMIT_EXPIRED,
    PERMIT_DUE,
    PERMIT_SUSPENDED,
    PERMIT_RESUMED,
    PERMIT_REVOKED,
    PERMIT_DUPLICATE_ISSUED,
    FOREST_TICKET_ISSUED,
    PERMIT_UNSIGNED_STALLED,
)
