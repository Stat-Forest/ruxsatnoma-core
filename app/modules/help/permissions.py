"""Permission codes owned by help; importing registers them (same idiom as
app/modules/gis/permissions.py). No role is seeded holding either grant yet —
an Agency org-chart question, filed as an open item rather than guessed here;
`sys_admin` reaches every route regardless (decision #41 ruling 2)."""

from app.modules.auth.permissions import register

FAQ_MANAGE = "help.faq.manage"
TICKETS_MANAGE = "help.tickets.manage"

register(
    {
        FAQ_MANAGE: "Create, edit and publish FAQ entries",
        TICKETS_MANAGE: "See every support ticket, assign it, and reply on anyone's behalf",
    }
)
