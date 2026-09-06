"""Permission codes owned by public; importing registers them (same idiom as
app/modules/gis/permissions.py). No role is seeded holding this grant yet —
which staff role should triage citizen appeals is an Agency org-chart
question, filed as an open item rather than guessed here; `sys_admin` reaches
every route regardless (decision #41 ruling 2)."""

from app.modules.auth.permissions import register

APPEALS_MANAGE = "public.appeals.manage"

register({APPEALS_MANAGE: "Triage and answer citizen appeals (obrashcheniya)"})
