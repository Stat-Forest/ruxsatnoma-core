"""Permission codes owned by applications; importing registers them (same idiom as
app/modules/gis/permissions.py; registration itself is a stand-in import in
app/main.py until branch 2's router.py exists and imports this module directly).

Migration 0015 grants each to a role (ruling 16, with one controller correction):
create -> applicant, review -> executor_staff ("hodim" in the plan's prose, tz/03
role 4), decide -> executor_head AND leadership, view_any -> prosecutor,
assign -> sys_admin.

`decide` on BOTH roles (review round 1, finding I3): tz/03's own permission matrix
(4-илова) gives "Т" (утверждение/подпись) on "Заявка" to Раҳбар, whose role code is
`executor_head` ("Ваколатли шахс") — decision #29's escalation ladder names the same
role ("эскалируется ваколатли шахсу вышестоящей организации"). Ruling 16's prose named
only `leadership` ("Агентлик раҳбарияти", agency-level), which tz/03's matrix gives
view+export only, not approve — granting `decide` to `leadership` alone would leave no
leshoz head able to approve or reject anything. `leadership` is kept alongside
`executor_head` because ruling 16 names it and the escalation ladder's agency tier
sits there; `norms.approve`'s own identical grant to `leadership` (migration 0011) is
a separate question the controller has taken upward, not one this migration revisits."""

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
        APPLICATIONS_DECIDE: "Approve or reject an application (leshoz head, agency leadership)",
        APPLICATIONS_VIEW_ANY: "See applications beyond one's own (staff, prosecutor)",
        APPLICATIONS_ASSIGN: "Reassign an application to another org or user",
    }
)
