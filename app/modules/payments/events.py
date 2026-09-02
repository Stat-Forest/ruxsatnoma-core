"""Notification event codes `payments` hands to `notifications.service.notify`.

DOTTED, past-participle codes — `notification_templates.event_code`'s own
vocabulary — NEVER one of the flat, underscore bus names in
`applications.events` (`APPLICATION_APPROVED` and friends). That module's own
docstring lays out why the two vocabularies must never be crossed: a bus event
name is what `core.events.subscribe`/`publish` match on, a notification event
code is what a seeded `notification_templates` row is looked up by, and they
happen to share the word "application"/"invoice" without being interchangeable.

`NOTIFIED_EVENT_CODES` is the registry of every code this module's service
layer may pass to `notify(..., event_code=...)` — one entry today
(`invoice.issued`, seeded by migration `0009_notifications.py` for both
`inapp` and `sms`); later tasks of this stage append to the tuple as they add
more (`payment.confirmed`, ...), never replace it.
"""

INVOICE_ISSUED = "invoice.issued"

NOTIFIED_EVENT_CODES = (INVOICE_ISSUED,)
