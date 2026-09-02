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
migration `0009_notifications.py` for both `inapp` and `sms`) and
`payment.confirmed` (Task 4, same migration, same two channels). Later tasks
of this stage append to the tuple, never replace it.

--- Bus event name — `core.events.publish`/`subscribe` --------------------

`PAYMENT_CONFIRMED` (task 4, ruling J) is the one event THIS module
publishes — on a successful Payme `PerformTransaction`
(`payme.py`/`service.confirm_payment`), payload `{"invoice_id": ...}` only,
mirroring `applications.events`'s own "nothing but the id" discipline (a
subscriber reads everything else through this module's public surface). No
subscriber exists yet in this branch — `app/event_subscriptions.py` notes
3.11 `permits` as the first one; publishing to an empty bus today is a
deliberate no-op, not a gap.
"""

INVOICE_ISSUED = "invoice.issued"
PAYMENT_CONFIRMED_NOTIFICATION_CODE = "payment.confirmed"

NOTIFIED_EVENT_CODES = (INVOICE_ISSUED, PAYMENT_CONFIRMED_NOTIFICATION_CODE)

PAYMENT_CONFIRMED = "payment_confirmed"
