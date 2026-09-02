"""Every permission code granted in `role_permissions` must be a registered key of
`app.modules.auth.permissions.PERMISSIONS` — the root-fix guard for a whole class of
bug, not just applications': a module's `permissions.py` registers its codes only as
an IMPORT side effect, and every other module gets imported by its own `router.py`
(itself imported at the top of `app/main.py`). A module shipped without a router yet
(applications, branch 1 of 3.9a) has nothing importing its `permissions.py`, so its
codes land in the database via a migration grant while `PERMISSIONS` never learns
about them — `admin.users_service.set_role_permissions` then rejects them with
`ERR-VAL-001 unknown_permission`, and `list_permissions` silently omits them from the
picker (review round 1, applications task 1, finding I1)."""

from sqlalchemy import text

# Importing app.main runs every router's module-level imports (plus the applications
# stand-in import main.py now carries), which is how each module's permissions.py
# actually registers its codes into PERMISSIONS.
import app.main  # noqa: F401
from app.modules.auth.permissions import PERMISSIONS


async def test_every_granted_permission_code_is_registered(db) -> None:
    rows = await db.execute(text("SELECT DISTINCT permission_code FROM role_permissions"))
    granted = {row[0] for row in rows}
    missing = granted - set(PERMISSIONS)
    assert not missing, f"granted in role_permissions but never registered: {sorted(missing)}"
