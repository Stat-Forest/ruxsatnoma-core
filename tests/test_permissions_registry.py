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

import pathlib

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


# The seven codes that are registered, required by a route, and granted to NO
# role: reachable only through the `sys_admin` superuser bypass (decision #41
# ruling 2).
#
# The first six predate stages 3.9-3.11 and are believed intentional —
# configuration of the system itself: who exists, what the classifiers say,
# what the announcements say, what the settings are, and whose session may be
# killed. No non-superuser role in `0003_auth` is given that by tz/03's matrix.
#
# Written down as an ALLOWLIST rather than left implicit, so that the set is a
# deliberate statement instead of an accident: a code that is registered and
# required by a route but reaches nobody is normally a permission whose grant was
# forgotten in the migration — a role that silently cannot do its job, which is
# exactly how 3.6a/3.7 gave contour and norm approval to the wrong role for two
# stages (decision #59). Adding a code here is therefore a decision, not a fix, and
# the equality below makes GRANTING one of these six a deliberate edit too.
UNGRANTED_BY_DESIGN = frozenset(
    {
        "admin.announcements.manage",
        "admin.classifiers.manage",
        # `0043`: the legal-documents register, the same class as the
        # announcements grant above it — editorial content whose owner in the
        # Agency's org chart nobody has named yet. Reachable through the
        # `sys_admin` bypass until an administrator grants it to a role.
        "admin.legal_documents.manage",
        "admin.organizations.manage",
        "admin.settings.manage",
        "auth.sessions.revoke_any",
        "auth.users.manage",
        # 4.6/4.8 (`public`/`help`, plan `04.6-4.8-public-help.md`): who triages
        # citizen appeals, FAQ content and support tickets is an Agency
        # org-chart question `tz/03`'s matrix does not answer — filed as an
        # open item rather than guessed at. Reachable only through the
        # `sys_admin` bypass until a role is chosen and this migration-grant
        # gap is closed deliberately.
        "public.appeals.manage",
        "help.faq.manage",
        "help.tickets.manage",
        # Stage 7.9 task 3 (decision #163): the split's recipients directory
        # decides where a country's money goes, so this one is a DELIBERATE,
        # permanent member of this set until the Agency names who besides
        # `sys_admin` may edit it — unlike the others above, granting it is
        # expected to stay a one-row `role_permissions` INSERT, never a code
        # change, whenever that day comes.
        "payments.recipients.manage",
    }
)


def _declared_permission_codes() -> set[str]:
    """The codes the APPLICATION registers, as opposed to whatever else is in the
    process-global `PERMISSIONS` dict by the time this test runs.

    `tests/modules/auth/test_me_rbac.py` registers `test.secret`/`test.secret2`
    at module import — deliberately, and it must stay that way (re-registering
    raises) — and collection imports it before this file. `PERMISSIONS` alone is
    therefore not a question about the product. A production code is one written
    down in some `app/**/permissions.py`; a throwaway one is not.
    """
    sources = sorted(pathlib.Path("app").rglob("permissions.py"))
    assert len(sources) >= 5, f"only {len(sources)} permissions.py files found — the walk is wrong"
    bodies = [path.read_text(encoding="utf-8") for path in sources]
    return {code for code in PERMISSIONS if any(f'"{code}"' in body for body in bodies)}


async def test_every_registered_permission_code_reaches_some_role(db) -> None:
    """The reverse of the guard above, which only ever asserted granted ⊆
    registered — so a code registered by a module and required by its routes
    could reach no role at all and nothing would say so.

    **Only `is_system` roles count**, i.e. the eleven `0003_auth` seeds. A role
    invented by a test or by an admin proves nothing about whether the PRODUCT
    ships a capability, and this test read `test.secret`/`test.secret2` as
    ungranted and `auth.users.manage` as granted on its first full-suite run —
    the former from the process-global registry, the latter from a throwaway
    role `tests/modules/admin/test_roles_admin.py` creates and COMMITS on this
    shared, persistent database (lesson: the test DB is never empty, including
    the spot you picked).

    `user_permissions` is deliberately not consulted either: a personal grant is
    an exception an admin makes for one person, and the question here is whether
    a ROLE can do its job.
    """
    rows = await db.execute(
        text(
            "SELECT DISTINCT rp.permission_code FROM role_permissions rp "
            "JOIN roles r ON r.id = rp.role_id WHERE r.is_system"
        )
    )
    granted = {row[0] for row in rows}
    ungranted = _declared_permission_codes() - granted

    assert ungranted == set(UNGRANTED_BY_DESIGN), (
        "the set of permission codes granted to no role has changed. A NEW one means a "
        "migration registered a code and forgot to grant it — the role silently cannot do "
        "its job, and only sys_admin's bypass hides it. A MISSING one means one of the six "
        "superuser-only codes was granted to a role; update the allowlist deliberately. "
        f"unexpected: {sorted(ungranted - UNGRANTED_BY_DESIGN)}, "
        f"no longer ungranted: {sorted(UNGRANTED_BY_DESIGN - ungranted)}"
    )


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


# Decision #95 (Oybek, 2026-09-06), answering `tz/12` #48: **the prosecutor reads
# everything and writes nothing.** The ruling's standing half is a rule about the
# FUTURE — "a module that adds a read permission adds it to `prosecutor` in the
# same migration" — and a rule about the future is worth exactly as much as the
# check that enforces it. This is that check.
#
# What made it necessary: the stage 7.3 walkthrough found `payments.view`
# ungranted (finding F20), so the prosecutor could read applications and permits
# and got a 403 on the money — which С22 lists among the things they inspect.
# Nothing failed; the register simply refused, in a role nobody exercises daily.
READ_CODE_SUFFIXES = (".view", ".view_any")

PROSECUTOR = "prosecutor"


async def _codes_of_role(db, role_code: str) -> set[str]:
    rows = await db.execute(
        text(
            "SELECT rp.permission_code FROM role_permissions rp "
            "JOIN roles r ON r.id = rp.role_id WHERE r.code = :role AND r.is_system"
        ).bindparams(role=role_code)
    )
    return {row[0] for row in rows}


async def test_the_prosecutor_holds_every_read_permission(db) -> None:
    """`search.use` and `dashboard.view` are held too; the suffix rule is what can
    be checked mechanically, and every code it matches must reach this role.

    A new `*.view`/`*.view_any` code that fails here is not this test being
    strict — it is a module having decided, silently, that oversight may not see
    its rows.
    """
    reads = {code for code in _declared_permission_codes() if code.endswith(READ_CODE_SUFFIXES)}
    assert len(reads) >= 8, (
        f"the suffix rule matched only {sorted(reads)} — has the naming changed?"
    )
    missing = reads - await _codes_of_role(db, PROSECUTOR)
    assert not missing, (
        f"read permissions the prosecutor does not hold: {sorted(missing)}. Decision #95: a module "
        "that adds a read permission grants it to `prosecutor` in the same migration."
    )


async def test_the_prosecutor_holds_no_permission_that_writes(db) -> None:
    """The other half of the same ruling, and the half a "grant the role more"
    change loses quietly. Anything not matching the read suffixes is a write or a
    capability; `search.use` is the one non-suffix read and is named here rather
    than left to a broader pattern that would let a real write slip through."""
    allowed_non_read = {"search.use"}
    held = await _codes_of_role(db, PROSECUTOR)
    writes = {c for c in held if not c.endswith(READ_CODE_SUFFIXES)} - allowed_non_read
    assert not writes, (
        f"the prosecutor is read-only (С22, decision #95) but holds: {sorted(writes)}"
    )
