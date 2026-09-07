"""Dotted `notification_templates.event_code` values this module notifies a
violator on (ruling #138, R1 of `docs/plans/07.6-handover-and-the-violator.md`
— finding F2 of `07.5-audit-findings.md`: a violation case told its violator
nothing, at any of its seven transitions).

**Four transitions notify, not seven.** The case's lifecycle is open ->
request-explanation -> explained -> decide -> appeal -> resolve-appeal ->
close; only the four where a clock starts or a fact about the citizen
changes are wired to `notifications.service.notify` (`_open_case`,
`request_explanation`, `decide_case`, `close_case`). `submit_explanation`,
`appeal_case` and `resolve_appeal` are performed BY the violator themselves
(or resolve an appeal without changing the decision already reported) and
stay deliberately silent — telling someone what they just did is noise, and
noise is what makes people stop reading the notifications that matter.

Same three-vocabulary split every module's own `events.py` states for
itself (`applications/events.py` carries the full table): these are
DOTTED, past-participle strings — what `notify(event_code=...)` looks a
template up by — never a bus name (this module publishes none of its own)
and never one of the present-tense `audit_log.action` constants defined
beside the service functions that call `audit.service.log`.

Why the tuple exists at all: with no template, `notify()` writes a raw,
untranslated fallback string in-app and sends NOTHING by SMS or e-mail,
silently, with one `notification.template_missing` ERROR log line — on
EVERY occurrence, forever. `tests/modules/inspections/
test_case_notifications.py::test_every_event_this_module_notifies_on_has_a_template`
asserts every code below has an ACTIVE template on every default channel;
migration `0038` seeds all four, in `uz_latn`/`uz_cyrl`/`ru`, in the same
commit this tuple was added.
"""

CASE_OPENED = "violation_case.opened"
CASE_EXPLANATION_REQUESTED = "violation_case.explanation_requested"
CASE_DECIDED = "violation_case.decided"
CASE_CLOSED = "violation_case.closed"

NOTIFIED_EVENT_CODES: tuple[str, ...] = (
    CASE_OPENED,
    CASE_EXPLANATION_REQUESTED,
    CASE_DECIDED,
    CASE_CLOSED,
)
