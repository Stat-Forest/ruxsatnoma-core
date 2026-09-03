"""Permission codes owned by applications; importing registers them (same idiom as
app/modules/gis/permissions.py). Registration happens through `router.py`, which
imports this module and is itself wired into `app/main.py` — the stand-in import
that stood in `main.py` while this module had no router is gone.

Migration 0015 grants each to a role (ruling 16, with one controller correction):
create -> applicant, review -> executor_staff ("hodim" in the plan's prose, tz/03
role 4), decide -> executor_head, view_any -> prosecutor, assign -> sys_admin.

`decide` belongs to `executor_head` and to it alone. tz/03's permission matrix
(4-илова) gives "Т" (утверждение/подпись) on "Заявка" to Раҳбар, whose role code is
`executor_head` ("Ваколатли шахс", the leshoz head) — decision #29's escalation
ladder names the same role ("эскалируется ваколатли шахсу вышестоящей организации").
Ruling 16's prose named `leadership` instead ("Агентлик раҳбарияти", agency-level),
which the matrix gives view+export only; 0015 shipped the grant on both roles as a
review-time correction, and **migration 0016 revoked `leadership`'s half** after
Oybek settled the question (decision #59, option а — the same migration moved
`norms.approve` and `gis.contours.approve` off `leadership` for the identical
reason). The escalation ladder loses nothing: each tier has its own
`executor_head`, reached through the organization hierarchy."""

from app.modules.auth.permissions import register

APPLICATIONS_CREATE = "applications.create"
APPLICATIONS_REVIEW = "applications.review"
APPLICATIONS_DECIDE = "applications.decide"
APPLICATIONS_VIEW_ANY = "applications.view_any"
APPLICATIONS_ASSIGN = "applications.assign"

register(
    {
        APPLICATIONS_CREATE: "File and edit one's own application (applicant)",
        APPLICATIONS_REVIEW: "Take an application into work and record checks (hodim)",
        APPLICATIONS_DECIDE: "Approve or reject an application (executor_head — the leshoz head)",
        APPLICATIONS_VIEW_ANY: "See applications beyond one's own (staff, prosecutor)",
        APPLICATIONS_ASSIGN: "Reassign an application to another org or user",
    }
)
