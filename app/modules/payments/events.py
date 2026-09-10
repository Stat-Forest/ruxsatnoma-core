"""Event names `payments` uses on its two vocabularies (ruling J, task 4).

DOTTED, past-participle codes — `notification_templates.event_code`'s own
vocabulary — NEVER one of the flat, underscore bus names in
`applications.events` (`APPLICATION_APPROVED` and friends). That module's own
docstring lays out why the two vocabularies must never be crossed: a bus event
name is what `core.events.subscribe`/`publish` match on, a notification event
code is what a seeded `notification_templates` row is looked up by, and they
happen to share the word "application"/"invoice"/"payment" without being
interchangeable. This file holds both, for the same module, so the split is
stated twice below rather than once — get the wrong one into the wrong call
and either `notify()` silently finds no template, or a future subscriber
listening on the bus never fires.

--- Notification event codes — `notify(event_code=...)` ------------------

`NOTIFIED_EVENT_CODES` is the registry of every code this module's service
layer may pass to `notify(...)`: `invoice.issued` (Task 2, seeded by
migration `0009_notifications.py` for both `inapp` and `sms`),
`payment.confirmed` (Task 4, same migration, same two channels) and
`invoice.due_soon` (Task 6, seeded by migration `0018` for both channels —
`0009` predates this module and could not have seeded it). 3.10b task 1 adds
`refund.decided` and `payment.manual_confirmed`, seeded by migration `0022`
for both channels. Later tasks of this stage append to the tuple, never
replace it.

--- Bus event name — `core.events.publish`/`subscribe` --------------------

`PAYMENT_CONFIRMED` (task 4, ruling J) is the one event THIS module
publishes — on a successful Payme `PerformTransaction`
(`payme.py`/`service.confirm_payment`). Its payload is exactly two
IDENTIFIERS and nothing else:

    {"invoice_id": str(invoice.id), "application_id": str(invoice.application_id)}

`application_id` is not optional decoration (whole-branch review). Ruling
15's discipline is "no second source of truth for money" — which is why
`applications.events` carries no amount and no number — NOT "one key per
payload": a subscriber holding only an `invoice_id` cannot reach anything
at all, because this module's frozen public surface
(`service.invoice_for_application`, `service.is_paid`) takes an
`application_id` in both directions and explicitly forbids a level-4+
caller from reading `invoices` as a table of its own. 3.11 `permits`'s own
subscriber reads `application_id` off this payload and returns early
without it, so dropping the key silently disables permit issuance for
EVERY payment rather than failing anywhere visible. Both values are ids;
neither is money.

Both values arrive as STRINGS, not `uuid.UUID` — matching `invoice_id`, which
has been serialized since task 4. A subscriber may hand either straight to a
service taking a `uuid.UUID` (`applications.service.get` accepts the string
form; SQLAlchemy's `Uuid` type coerces it — verified, not assumed), or
normalize it the way `subscribers.py::_application_id` already does for the
`applications` events, which occur in both shapes.

A subscriber still reads everything else — the number, and above all the
amount — through the public surface, never off the event.

No subscriber exists yet in this branch — `app/event_subscriptions.py`
notes 3.11 `permits` as the first one; publishing to an empty bus today is
a deliberate no-op, not a gap.
"""

INVOICE_ISSUED = "invoice.issued"
PAYMENT_CONFIRMED_NOTIFICATION_CODE = "payment.confirmed"
INVOICE_DUE_SOON = "invoice.due_soon"
REFUND_DECIDED = "refund.decided"
PAYMENT_MANUAL_CONFIRMED = "payment.manual_confirmed"
# Ruling #185 (stage 10, track B4): `service._settle_free` sends this INSTEAD
# of `INVOICE_ISSUED` above when a zero-sum invoice settles itself — the
# applicant is told there is nothing to pay, never asked to pay it. Its
# template is seeded by migration `0054`, added at the same commit that
# registers this code below (the same discipline `PAYMENT_REVERSED`'s own
# comment states).
INVOICE_SETTLED_BY_BENEFIT = "invoice.settled_by_benefit"
# Ruling #202: the same "nothing to pay" notice for a statutory exemption
# (`science`), in the law's own words rather than the benefit template's
# «imtiyozi qo'llanildi». Template seeded by migration `0058`.
INVOICE_SETTLED_BY_LAW = "invoice.settled_by_law"
# Ruling #112, `record_reversal`'s notify to the leshoz that a live permit's
# money just went back. Its template is seeded by migration `0036`, added at
# integration in the same commit that registered this code below — a
# notification whose whole purpose is that a human reads it cannot be the one
# arriving through `notifications.service`'s ruling-10 fallback (a raw,
# untranslated row plus a `notification.template_missing` ERROR on every
# reversal).
PAYMENT_REVERSED = "payment.reversed"

NOTIFIED_EVENT_CODES = (
    INVOICE_ISSUED,
    PAYMENT_CONFIRMED_NOTIFICATION_CODE,
    INVOICE_DUE_SOON,
    REFUND_DECIDED,
    PAYMENT_MANUAL_CONFIRMED,
    PAYMENT_REVERSED,
    INVOICE_SETTLED_BY_BENEFIT,
    INVOICE_SETTLED_BY_LAW,
)

PAYMENT_CONFIRMED = "payment_confirmed"
