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


async def test_leadership_holds_no_approval_code(db) -> None:
    """`tz/03`'s permission matrix (4-илова) gives «Т» — утверждение/подпись — to
    «Раҳбар», seeded by `0003_auth` as `executor_head` («Ваколатли шахс», the leshoz
    head). «Руководство» — `leadership`, «Агентлик раҳбарияти» — holds «К,Э», view
    and export, and nothing more.

    `.claude/lessons.md` asserted the opposite from 3.6a until 2026-09-02, so
    migrations 0010 and 0011 granted approval to `leadership` and the leshoz head
    could approve neither a contour nor a norm in its own leshoz. Migration 0016
    corrected it (decision #59). This guard is the mechanical half of that fix: the
    conflation was invisible for two stages precisely because nothing checked, and a
    reviewer reading one migration cannot see the matrix.

    `norms.publish` is the deliberate exception and is asserted as such below — ВМҚ
    689 has forest-pasture norms approved at the level of the forestry authority, so
    agency leadership is a defensible holder there (3.7 ruling 16)."""
    rows = await db.execute(
        text(
            "SELECT rp.permission_code FROM role_permissions rp "
            "JOIN roles r ON r.id = rp.role_id WHERE r.code = 'leadership'"
        )
    )
    held = {row[0] for row in rows}
    approval_codes = {c for c in held if c.endswith((".approve", ".decide"))}
    assert not approval_codes, (
        "leadership holds approval codes tz/03 gives to executor_head: "
        f"{sorted(approval_codes)} — see migration 0016 and decision #59"
    )
    assert "norms.publish" in held, (
        "leadership must KEEP norms.publish (VMQ 689, 3.7 ruling 16) — if this "
        "grant was removed, it was not by decision #59"
    )


async def test_executor_head_can_approve_what_tz03_says_it_can(db) -> None:
    """The positive half: «Раҳбар» holds «Т» on every object in the matrix, so the
    leshoz head must be able to approve a contour, a norm and an application. A
    revoke-only migration would have left nobody able to approve at all."""
    rows = await db.execute(
        text(
            "SELECT rp.permission_code FROM role_permissions rp "
            "JOIN roles r ON r.id = rp.role_id WHERE r.code = 'executor_head'"
        )
    )
    held = {row[0] for row in rows}
    for code in ("gis.contours.approve", "norms.approve", "applications.decide"):
        assert code in held, f"executor_head must hold {code} (tz/03 4-илова, decision #59)"
