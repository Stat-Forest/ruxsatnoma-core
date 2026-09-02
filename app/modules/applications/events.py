"""The four names `applications` publishes on the process bus (`app.core.events`),
plan 03.9a ruling 4 — no catch-all `application_status_changed`: a subscriber that
filters a generic event by status is a `switch` living in the wrong module, and the
event list is supposed to be readable as the process itself.

Three vocabularies share the "application" word in this codebase and must not be
confused — branch 2's `submit()` will use all three, side by side, for the very same
moment:

  - **Bus event names (HERE).** Flat snake_case, no dot: `application_submitted`.
    What `core.events.publish`/`subscribe` match on (ruling 3а — handlers run
    synchronously, inside the publisher's own transaction).
  - **`notification_templates.event_code`** (seeded by migration
    `0009_notifications.py`: `application.submitted`, `.approved`, `.rejected`).
    Dotted, past participle after the dot. What
    `notifications.service.notify(..., event_code=...)` looks a template up by.
  - **`audit_log.action`** (ruling 17, decision #38: `APPLICATION_SUBMIT =
    "application.submit"`, `.approve`, `.reject`, `.cancel`, ...). Dotted,
    present-tense verb after the dot. What `audit.service.log(..., action=...)`
    records — audit is level 0 and knows no domain vocabulary, so the constant
    lives with the acting module, same as here.

A single call to `submit()` therefore writes three different strings for one event:
`action="application.submit"` (audit), `event_code="application.submitted"`
(notify), `Event(name=APPLICATION_SUBMITTED)` (this bus) — none of them
interchangeable with either of the others, and the underscore/dot split is the
tell: if it has a dot, it is not one of the four names below. Never pass one of
these four constants where an `event_code` or an `action` is expected, or vice
versa; never seed a notification template under one of these four literal values.

Payload contract — what a subscriber may assume is present. Branch 2 is what
actually calls `publish` for these four names (this task ships the bus and these
names only, no publisher — see plan 03.9a task 2 "Scope"), so this is the contract
it commits to, not something exercised end-to-end here:

    APPLICATION_SUBMITTED  -- application_id, number (the public number just assigned)
    APPLICATION_APPROVED   -- application_id, amount (3.10a prices the invoice from this)
    APPLICATION_REJECTED   -- application_id
    APPLICATION_CANCELLED  -- application_id

`application_id` is the one key every one of the four guarantees. A subscriber must
not assume any key beyond what is listed for its own event name, and must tolerate
extra keys appearing when branch 2 lands without breaking.
"""

APPLICATION_SUBMITTED = "application_submitted"
APPLICATION_APPROVED = "application_approved"
APPLICATION_REJECTED = "application_rejected"
APPLICATION_CANCELLED = "application_cancelled"
