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
from app.modules.admin import repo as admin_repo
from app.modules.auth.models import User
from app.modules.beekeepers import repo as beekeepers_repo
from app.modules.norms.models import Tariff
from app.seed.demo import (
    DEMO_APPLICANT,
    DEMO_BEEKEEPERS,
    DEMO_STAFF,
    DemoUser,
    _ensure_beekeepers,
    _ensure_benefit_modifiers,
    _ensure_user,
)


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


async def test_a_renamed_login_converges_onto_the_existing_row_by_pinfl(
    db: AsyncSession,
) -> None:
    """Migration 0053's own shape: `demo_benefit_verifier` became `demo_
    beekeeping_registrar`, same pinfl. A database that already ran the OLD
    seed has a row under the old login; re-seeding with the NEW spec must
    converge that SAME row (renaming its login) rather than trying to INSERT
    a second row sharing the pinfl and failing on `users_pinfl_key`."""
    pinfl = unique_pinfl()
    old_spec = DemoUser(
        login=f"seed-demo-test-old-{uuid.uuid4().hex[:8]}",
        full_name="Seed Demo Rename Test",
        role_code="sys_admin",
        pinfl=pinfl,
        password="Sinov#Seed4821",
    )
    old_user, _, created, _ = await _ensure_user(db, old_spec, shared_secret="JBSWY3DPEHPK3PXP")
    await db.commit()
    assert created is True

    new_spec = DemoUser(
        login=f"seed-demo-test-new-{uuid.uuid4().hex[:8]}",
        full_name="Seed Demo Rename Test",
        role_code="sys_admin",
        pinfl=pinfl,
        password="Sinov#Seed4821",
    )
    renamed_user, _, created_again, converged = await _ensure_user(
        db, new_spec, shared_secret="JBSWY3DPEHPK3PXP"
    )
    await db.commit()

    assert created_again is False
    assert converged is True
    assert renamed_user.id == old_user.id
    stored = await db.scalar(select(User).where(User.id == old_user.id))
    assert stored is not None
    assert stored.login == new_spec.login
    assert stored.pinfl == pinfl


async def test_ensure_beekeepers_is_idempotent(db: AsyncSession) -> None:
    """Two runs create the three rows once and change nothing the second
    time — one of them on `DEMO_APPLICANT`'s own pinfl (ruling #182's
    automatic path)."""
    actor, _, _, _ = await _ensure_user(
        db,
        DemoUser(
            login=f"seed-demo-registrar-{uuid.uuid4().hex[:8]}",
            full_name="Seed Demo Registrar",
            role_code="sys_admin",
            pinfl=unique_pinfl(),
            password="Sinov#Seed4821",
        ),
        shared_secret="JBSWY3DPEHPK3PXP",
    )
    await db.commit()

    message_first = await _ensure_beekeepers(db, actor=actor)
    await db.commit()
    assert message_first.startswith("Beekeepers register:")

    on_applicant_pinfl = [b for b in DEMO_BEEKEEPERS if b.pinfl == DEMO_APPLICANT.pinfl]
    assert len(on_applicant_pinfl) == 1
    row = await beekeepers_repo.get_active_by_certificate_no(
        db, on_applicant_pinfl[0].certificate_no
    )
    assert row is not None
    assert row.pinfl == DEMO_APPLICANT.pinfl

    message_second = await _ensure_beekeepers(db, actor=actor)
    await db.commit()
    assert message_second == "Beekeepers register: all three demo rows already present"


async def test_ensure_benefit_modifiers_is_idempotent(db: AsyncSession) -> None:
    """Sets '0' on every published apiary/recreation tariff row exactly once;
    a second run reports the already-set state and changes no row."""
    await _ensure_benefit_modifiers(db)
    await db.commit()

    for activity_code, codes in (
        ("apiary", ["beekeeping_union_member"]),
        (
            "recreation",
            [
                "preschool_children",
                "education_institutions",
                "orphanage_residents",
                "persons_with_disabilities",
                "war_veterans",
                "radiation_victims",
            ],
        ),
    ):
        activity = next(
            a for a in await admin_repo.list_activity_types(db) if a.code == activity_code
        )
        rows = (
            (
                await db.execute(
                    select(Tariff).where(
                        Tariff.activity_type_id == activity.id, Tariff.status == "published"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows, f"expected at least one published {activity_code} tariff"
        for row in rows:
            for code in codes:
                assert (row.benefit_modifiers or {}).get(code) == "0"

    message_second = await _ensure_benefit_modifiers(db)
    assert message_second.startswith("Benefit modifiers: the apiary/recreation tariffs already")


def test_no_two_demo_accounts_share_a_password() -> None:
    """Every account carries its OWN fixed password (Oybek, 2026-09-04): the
    demo runs on the internet-facing dev server, where one string opening all
    eleven accounts — `sys_admin` among them — is not acceptable.

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
    would then hand over the other ten. Cheap structural guard — the login's
    distinctive part must not appear in its own password."""
    for spec in (*DEMO_STAFF, DEMO_APPLICANT):
        stem = spec.login.removeprefix("demo_")
        assert stem.lower() not in spec.password.lower(), spec.login
