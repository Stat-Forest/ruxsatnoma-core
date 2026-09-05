"""Permission codes owned by oversight; importing registers them (same idiom
as `app/modules/gis/permissions.py`).

`oversight.view` is granted (migration 0028) to the roles the matrix (`tz/03`)
already gives audit-log/RI-adjacent visibility to: `central_admin`,
`leadership`, `executor_head` (zone-scoped to their own organization) and
`prosecutor` (С22 — the read-only surface this module exists for).
`sys_admin` needs no explicit grant: it is a superuser and `require_permission`
lets it through before checking codes (decision #41 ruling 2)."""

from app.modules.auth.permissions import register

OVERSIGHT_VIEW = "oversight.view"

register(
    {
        OVERSIGHT_VIEW: (
            "View accumulated risk indicators and oversight events "
            "(central office, leadership, leshoz head, prosecutor)"
        ),
    }
)
