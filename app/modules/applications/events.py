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
it commits to, not something exercised end-to-end here: every one of the four
carries `application_id` and NOTHING else. That is deliberate, not a placeholder —
it holds even for a value that looks convenient to carry along, such as the public
`number` assigned at submission or, on `APPLICATION_APPROVED`, an amount to
invoice (controller ruling, review round 1, finding I4 — an earlier draft of this
docstring promised both).

A subscriber gets everything else about the application through the module's own
public surface once it has `application_id` — `applications.service.get(db,
application_id)` for the row itself (including `number`),
`applications.service.current_calculation(db, application_id)` for the priced
amount — never through the event body. For the amount this is not a style
preference: a figure carried on the event would be a SECOND source of truth for
money alongside the stored calculation, exactly the defect class stage 3.7's split
between `preview` (priced, disposable) and `save_calculation` (priced, stored)
exists to prevent — 3.10a's invoice handler must read `current_calculation`,
never `event.payload`. The same rule then drops `number` too, even though it
carries no such risk: a contract that keeps one field because it is "convenient"
while dropping another on separate grounds is one every future event has to
relitigate on its own; "`application_id` only, everything else through the public
surface" is not.

A subscriber must not assume any key beyond `application_id` is present, and must
tolerate extra keys appearing later without breaking.
"""

APPLICATION_SUBMITTED = "application_submitted"
APPLICATION_APPROVED = "application_approved"
APPLICATION_REJECTED = "application_rejected"
APPLICATION_CANCELLED = "application_cancelled"


# --- The notification registry (task 8, ruling 26) ----------------------------
#
# Every DOTTED `notification_templates.event_code` this module passes to
# `notifications.service.notify()`, and the one place a new notification has to
# be registered. NOT the four bus constants above: those are flat snake_case,
# match no template, and `notify()` answers an unknown code by writing a raw
# fallback string in-app and sending NOTHING by SMS or e-mail — silently, with
# one log line. Two tests in `tests/modules/applications/test_end_to_end.py`
# hold the pair together: one asserts every code here has an active `inapp`
# template seeded, the other that every `NOTIFY_*` constant in this module
# appears here, so an unregistered notification fails CI instead of going out
# blank.
#
# `application.cancelled` is deliberately ABSENT: `cancel` sends no
# notification at all (no such template is seeded), so listing it here would
# make the first test fail for a message nobody sends. The day cancellation
# notifies, the template and this entry land in the same commit.
NOTIFIED_EVENT_CODES = (
    "application.submitted",
    "application.approved",
    "application.rejected",
)
