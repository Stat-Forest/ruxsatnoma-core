"""`app.seed.demo._ensure_user` — the convergence guarantee its own docstring
promises: an already-existing row's `pinfl`, `organization_id` AND password
are forced back to the seeded spec, not merely left alone.

Only the password half is exercised here. `pinfl`/`organization_id`
convergence already has no test of its own either, but this file exists for
one regression: the verification run one hour before the demo found
`demo_chief_forester` not accepting its seeded password after a reseed, because
`_ensure_user`'s existing-row branch never rewrote `password_hash` at all —
the docstring's "regardless of whether the row already existed" was true of
`pinfl`/`organization_id` and false of the password.

Never against `DATABASE_URL` (the shared dev database `app/seed/demo.py`
itself targets) — the `db` fixture from `tests/conftest.py` already points at
the test database, and this file adds no override of its own.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.modules.auth.models import User
from app.seed.demo import DEMO_APPLICANT, DEMO_STAFF, DemoUser, _ensure_user


def unique_pinfl() -> str:
    """14 digits (`pinfl_format` CHECK), leading `9` so a run of this file
    never collides with a real demo account's `3026090400...` prefix or
    another module's fixtures on this shared, persistent test DB."""
    return f"9{uuid.uuid4().int % 10**13:013d}"


def _spec() -> DemoUser:
    """`sys_admin`, org-less: the point is the password branch alone, and an
    `organization_code` would pull in `admin_repo.get_organization_by_code`
    for a fact this test does not need."""
    return DemoUser(
        login=f"seed-demo-test-{uuid.uuid4().hex[:12]}",
        full_name="Seed Demo Password Test",
        role_code="sys_admin",
        pinfl=unique_pinfl(),
        password="Sinov#Seed4821",
    )


async def test_password_converges_after_a_manual_change(db: AsyncSession) -> None:
    """The exact defect: a row whose `password_hash` was overwritten by hand
    (or by anything else) is put back on `DEMO_PASSWORD` the next time the
    seed runs, and the run reports it as converged."""
    spec = _spec()

    created_user, _, created, converged_at_creation = await _ensure_user(
        db, spec, shared_secret="JBSWY3DPEHPK3PXP"
    )
    await db.commit()
    assert created is True
    assert converged_at_creation is False
    assert created_user.password_hash is not None
    assert verify_password(spec.password, created_user.password_hash)

    # Simulate the drift the verification run actually hit — a hash that is
    # no longer the seeded password's, written outside the seed script.
    created_user.password_hash = hash_password("SomeOtherPassword#1")
    await db.commit()

    reseeded_user, _, created_again, converged = await _ensure_user(
        db, spec, shared_secret="JBSWY3DPEHPK3PXP"
    )
    await db.commit()

    assert created_again is False
    assert converged is True
    assert reseeded_user.id == created_user.id
    # Read back from the database, not the in-memory object the fixture
    # mutated directly above — the row Postgres actually stored is what a
    # login attempt would check against.
    stored = await db.scalar(select(User).where(User.id == created_user.id))
    assert stored is not None
    assert stored.password_hash is not None
    assert verify_password(spec.password, stored.password_hash)


async def test_reseeding_an_unchanged_password_reports_no_convergence(
    db: AsyncSession,
) -> None:
    """The other half of the same guarantee: converging is not "always
    rewrite" — a row already on its seeded password is reported as merely
    existing, the same way an unchanged `pinfl`/`organization_id` is, so a
    routine reseed does not audit-log a no-op change on every account, every
    run."""
    spec = _spec()
    await _ensure_user(db, spec, shared_secret="JBSWY3DPEHPK3PXP")
    await db.commit()

    _, _, created_again, converged = await _ensure_user(db, spec, shared_secret="JBSWY3DPEHPK3PXP")
    await db.commit()

    assert created_again is False
    assert converged is False


def test_no_two_demo_accounts_share_a_password() -> None:
    """Every account carries its OWN fixed password (Oybek, 2026-09-04): the
    demo runs on the internet-facing dev server, where one string opening all
    eight accounts — `sys_admin` among them — is not acceptable.

    This asserts the property rather than the strings, so it survives a
    rotation but fails the moment an edit collapses two accounts onto one
    credential. `_ensure_user` reads `spec.password`, so such a collapse would
    otherwise be invisible: every login would still work."""
    specs = [*DEMO_STAFF, DEMO_APPLICANT]
    passwords = [spec.password for spec in specs]

    assert all(passwords), "every demo account needs a password"
    assert len(set(passwords)) == len(passwords), "two demo accounts share a password"


def test_no_demo_password_is_derivable_from_its_login() -> None:
    """The passwords must not be a visible function of the login: knowing one
    would then hand over the other seven. Cheap structural guard — the login's
    distinctive part must not appear in its own password."""
    for spec in (*DEMO_STAFF, DEMO_APPLICANT):
        stem = spec.login.removeprefix("demo_")
        assert stem.lower() not in spec.password.lower(), spec.login
