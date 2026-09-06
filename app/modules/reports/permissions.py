"""Permission codes owned by reports; importing registers them (the same idiom
as `app/modules/permits/permissions.py` and `app/modules/norms/permissions.py`).

Migration 0027 grants each to a role, straight out of `tz/03`'s 4-ilova matrix
row "Отчёты" (plan `04.3-reports` ruling 2) — every role code below was read
out of `0003_auth.py`, never out of scenario prose (lesson: there is no
`rahbar` code):

  * `reports.view`         -> everyone the matrix marks "К" for this row:
    `central_admin`, `leadership`, `executor_staff`, `gis_specialist`,
    `executor_head`, `accountant`, `prosecutor`. Covers both the form catalog
    and report instances (zone-scoped separately by `repo.zone_filter`).
  * `reports.manage`       -> `central_admin`, `executor_staff`, `accountant`
    ("Я,Ў" in the matrix) — create a report, (re)generate its rows, edit them
    by hand, submit, and start a post-approval revision.
  * `reports.sign`         -> `executor_head` ("Т" — the rahbar's ERI signature
    or a return, at the leshoz level).
  * `reports.accept`       -> `central_admin` — the final accept or return at
    the central-office level, and archiving/activating a `report_forms` row.
  * `reports.forms.manage` -> `central_admin` alone — С20: "Центр создаёт
    форму отчёта".
"""

from app.modules.auth.permissions import register

REPORTS_VIEW = "reports.view"
REPORTS_MANAGE = "reports.manage"
REPORTS_SIGN = "reports.sign"
REPORTS_ACCEPT = "reports.accept"
REPORTS_FORMS_MANAGE = "reports.forms.manage"

register(
    {
        REPORTS_VIEW: "View report forms and submitted reports (zone-scoped)",
        REPORTS_MANAGE: "Create, generate and edit a report; submit for signature",
        REPORTS_SIGN: "Sign (ERI) or return a report at the leshoz level (executor_head)",
        REPORTS_ACCEPT: "Accept or return a report at the central-office level (central_admin)",
        REPORTS_FORMS_MANAGE: "Create, activate and archive report forms (central_admin)",
    }
)
