"""Permission codes owned by permits; importing registers them (the same idiom as
`app/modules/applications/permissions.py` and `app/modules/gis/permissions.py`).

Migration 0019 grants each to a role. Every role code below was read out of
`0003_auth.py`, never out of plan prose — a wrong code inserts zero rows silently and
the grant reaches nobody (lesson):

  * `permits.issue`    -> `executor_staff` — «ходим», tz/03 role 4, who forms the
    document (`design/03` makes issuance a human act).
  * `permits.sign`     -> `executor_head`, `chief_forester`, `accountant` and
    `applicant` — the 3+1 signatories of `tz/13`'s four signature lines, and exactly
    the four purposes of ruling 4. `chief_forester` exists for this second signature
    and no other (decision #32).
  * `permits.view_any` -> `executor_staff` and `prosecutor` — seeing permits beyond
    one's own. Zone scoping is a SEPARATE control and both are still subject to it
    (lesson: a permission answers "at all", `zone_filter` answers "on whose rows").
  * `permits.manage`   -> `executor_head` — 3.11b's suspend / resume / revoke /
    duplicate. Registered now and unused by 3.11a, so 3.11b needs no migration of its
    own for it.

`permits.manage` goes to `executor_head` and NOT to `leadership`: suspending or
revoking an issued permit is an authoritative act on a single document, and tz/03's
matrix gives «Т» to «Раҳбар» = `executor_head` («Ваколатли шахс», the leshoz head),
while `leadership` («Агентлик раҳбарияти») holds view+export. Migration 0016 corrected
exactly this conflation project-wide two days ago (decision #59)."""

from app.modules.auth.permissions import register

PERMITS_ISSUE = "permits.issue"
PERMITS_SIGN = "permits.sign"
PERMITS_VIEW_ANY = "permits.view_any"
PERMITS_MANAGE = "permits.manage"

register(
    {
        PERMITS_ISSUE: "Form a permit from a paid application (hodim)",
        PERMITS_SIGN: "Sign a permit (executor_head, chief_forester, accountant, recipient)",
        PERMITS_VIEW_ANY: "See permits beyond one's own (staff, prosecutor)",
        PERMITS_MANAGE: "Suspend, revoke or duplicate an issued permit (executor_head)",
    }
)
