"""Permission code owned by beekeepers; importing registers it (same idiom
as app/modules/norms/permissions.py). One code for the whole register —
list, create, patch and remove are all the same action from the registrar's
side, and there is no separate read-only role to split it for (ruling #182:
the Union's own employee, central, holding this and nothing else)."""

from app.modules.auth.permissions import register

BEEKEEPERS_MANAGE = "beekeepers.manage"

register(
    {
        BEEKEEPERS_MANAGE: (
            "Manage the Beekeeping Union's certificate-holder register "
            "(beekeeping_registrar — the Union's own employee, central)"
        ),
    }
)
