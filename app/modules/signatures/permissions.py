"""Permission codes owned by `signatures` (design/02: each module owns its codes)."""

from app.modules.auth import permissions as auth_permissions

VIEW_ANY = "signatures.view_any"
REVERIFY = "signatures.reverify"

auth_permissions.register(
    {
        VIEW_ANY: "Read any object's signatures regardless of ownership (oversight, 4.2)",
        REVERIFY: "Re-run verification of a stored signature and record a new result",
    }
)
