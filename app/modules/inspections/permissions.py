"""Permission codes owned by `inspections`; importing registers them (the same
idiom as `permits/permissions.py`, `gis/permissions.py`). Migration `0026` grants
each to a role — every role code below was read out of `0003_auth.py`, never out
of plan prose (lesson: a wrong code inserts zero rows silently):

  * `inspections.tasks.manage`      -> `executor_staff`, `executor_head` — the
    site visit of C6 is raised during application review (ходим); a field
    inspection of C15 is assigned by the leshoz head.
  * `inspections.acts.write`        -> `inspector` — creates, updates and signs
    their own field act (tz/03: "Инспектор" holds Я,Т on "Акт инспекции").
  * `inspections.view_any`          -> `executor_head`, `central_admin`,
    `leadership`, `prosecutor` — reading beyond one's own zone/assignment.
  * `inspections.cases.manage`      -> `executor_head` — the raxbar's decision
    on a violation case (tz/03: "Раҳбар" holds Т on "Дело о нарушении").
  * `inspections.checklists.manage` -> `central_admin` — the checklist builder,
    the same owner tz/03 gives the report-forms builder.

`inspector` is also added by migration `0026` to `permits.view_any` (plan
ruling 3) — tz/03 gives inspectors read access to permits, and migration
`0019` never granted it. That grant belongs to `permits`' own permission code,
but the ROLE_GRANTS list recording it lives in this module's migration since
this is the module that found and needs the gap closed.
"""

from app.modules.auth.permissions import register

TASKS_MANAGE = "inspections.tasks.manage"
ACTS_WRITE = "inspections.acts.write"
VIEW_ANY = "inspections.view_any"
CASES_MANAGE = "inspections.cases.manage"
CHECKLISTS_MANAGE = "inspections.checklists.manage"

register(
    {
        TASKS_MANAGE: "Create/cancel inspection assignments (hodim, executor_head)",
        ACTS_WRITE: "Create, update and sign one's own field act (inspector)",
        VIEW_ANY: "See tasks/acts/cases beyond one's own (executor_head, central "
        "apparatus, leadership, prosecutor)",
        CASES_MANAGE: "Decide a violation case, resolve its appeal, close it (executor_head)",
        CHECKLISTS_MANAGE: "Author checklist versions (central apparatus)",
    }
)
