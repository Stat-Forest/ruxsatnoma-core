"""Permission codes owned by applications; importing registers them (same idiom as
app/modules/gis/permissions.py). Migration 0015 grants each to one role (ruling 16):
create -> applicant, review -> executor_staff ("hodim" in the plan's prose,
tz/03 role 4), decide -> leadership, view_any -> prosecutor, assign -> sys_admin."""

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
        APPLICATIONS_DECIDE: "Approve or reject an application (the deciding head)",
        APPLICATIONS_VIEW_ANY: "See applications beyond one's own (staff, prosecutor)",
        APPLICATIONS_ASSIGN: "Reassign an application to another org or user",
    }
)
