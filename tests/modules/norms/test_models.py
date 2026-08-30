"""The four tables of stage 3.7 and the invariants the database itself enforces."""

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import uuid7
from app.modules.auth.models import User
from app.modules.gis.models import Contour

pytestmark = pytest.mark.asyncio


async def test_two_published_parameters_with_the_same_code_cannot_overlap(
    db: AsyncSession, gis_user: User, checker_user: User, unique_suffix: str
) -> None:
    code = f"test_param_{unique_suffix}"
    for effective_from, effective_to in ((date(2025, 1, 1), date(2025, 12, 31)),):
        await db.execute(
            text(
                "INSERT INTO rule_parameters (id, code, value, effective_from, effective_to, "
                "basis, status, created_by, approved_by) VALUES "
                "(:id, :code, to_jsonb('1'::text), :ef, :et, 'test', 'published', :m, :c)"
            ).bindparams(
                id=uuid7(),
                code=code,
                ef=effective_from,
                et=effective_to,
                m=gis_user.id,
                c=checker_user.id,
            )
        )
    with pytest.raises(IntegrityError):
        await db.execute(
            text(
                "INSERT INTO rule_parameters (id, code, value, effective_from, effective_to, "
                "basis, status, created_by, approved_by) VALUES "
                "(:id, :code, to_jsonb('2'::text), :ef, :et, 'test', 'published', :m, :c)"
            ).bindparams(
                id=uuid7(),
                code=code,
                ef=date(2025, 6, 1),
                et=None,
                m=gis_user.id,
                c=checker_user.id,
            )
        )
    await db.rollback()


async def test_a_draft_parameter_may_overlap_a_published_one(
    db: AsyncSession, gis_user: User, checker_user: User, unique_suffix: str
) -> None:
    """The EXCLUDE is partial (`WHERE status = 'published'`): drafting the next
    version of a parameter while the current one is in force is the normal way
    to work, and must not be refused."""
    code = f"test_param_{unique_suffix}"
    await db.execute(
        text(
            "INSERT INTO rule_parameters (id, code, value, effective_from, basis, status, "
            "created_by, approved_by) VALUES "
            "(:id, :code, to_jsonb('1'::text), :ef, 'test', 'published', :m, :c)"
        ).bindparams(id=uuid7(), code=code, ef=date(2025, 1, 1), m=gis_user.id, c=checker_user.id)
    )
    await db.execute(
        text(
            "INSERT INTO rule_parameters (id, code, value, effective_from, basis, status, "
            "created_by) VALUES (:id, :code, to_jsonb('2'::text), :ef, 'test', 'draft', :m)"
        ).bindparams(id=uuid7(), code=code, ef=date(2025, 6, 1), m=gis_user.id)
    )
    await db.flush()
    await db.rollback()


async def test_publishing_a_parameter_approved_by_its_own_maker_is_refused_by_the_database(
    db: AsyncSession, gis_user: User, unique_suffix: str
) -> None:
    """Ruling 10: the service refuses this first, but the CHECK must bite on its
    own — a mirrored constraint that only the service ever exercises rots."""
    with pytest.raises(IntegrityError):
        await db.execute(
            text(
                "INSERT INTO rule_parameters (id, code, value, effective_from, basis, status, "
                "created_by, approved_by) VALUES "
                "(:id, :code, to_jsonb('1'::text), :ef, 'test', 'published', :u, :u)"
            ).bindparams(
                id=uuid7(),
                code=f"test_param_{unique_suffix}",
                ef=date(2025, 1, 1),
                u=gis_user.id,
            )
        )
    await db.rollback()


async def test_a_row_seeded_by_a_migration_may_be_published_without_a_checker(
    db: AsyncSession, unique_suffix: str
) -> None:
    """Ruling 10's exemption: migration 0012 seeds the VMQ tariffs and parameters
    with `created_by IS NULL`, and those rows must be publishable — otherwise a
    fresh database ships with nothing in force."""
    await db.execute(
        text(
            "INSERT INTO rule_parameters (id, code, value, effective_from, basis, status) "
            "VALUES (:id, :code, to_jsonb('1'::text), :ef, 'test seed', 'published')"
        ).bindparams(id=uuid7(), code=f"test_seed_{unique_suffix}", ef=date(2025, 1, 1))
    )
    await db.flush()
    await db.rollback()


async def test_a_published_norm_needs_an_approval_document(
    db: AsyncSession,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    gis_user: User,
) -> None:
    with pytest.raises(IntegrityError):
        await db.execute(
            text(
                "INSERT INTO norms (id, contour_id, activity_type_id, yield_c_per_ha, "
                "effective_from, status, created_by) VALUES "
                "(:id, :contour, :activity, 12.0, :ef, 'published', :u)"
            ).bindparams(
                id=uuid7(),
                contour=published_contour.id,
                activity=grazing_activity_id,
                ef=date(2025, 1, 1),
                u=gis_user.id,
            )
        )
    await db.rollback()


async def test_a_calculation_cannot_be_updated_or_deleted(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """Ruling 21. The UPDATE is scoped to the row this test inserted — the test
    database is shared (lesson)."""
    calculation_id = uuid7()
    await db.execute(
        text(
            "INSERT INTO calculations (id, contour_id, activity_type_id, rule_code_version, "
            "input_snapshot, amount, breakdown) VALUES "
            "(:id, :contour, :activity, 'norms-1.0.0', '{}'::jsonb, 0, '[]'::jsonb)"
        ).bindparams(id=calculation_id, contour=published_contour.id, activity=grazing_activity_id)
    )
    await db.commit()
    # `DBAPIError` with a message match, the way tests/modules/audit/test_audit_log.py
    # asserts its own append-only trigger — the plpgsql RAISE surfaces as a
    # DBAPIError subclass and matching on the text proves it was OUR trigger.
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            text("UPDATE calculations SET amount = 1 WHERE id = :id").bindparams(id=calculation_id)
        )
    await db.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            text("DELETE FROM calculations WHERE id = :id").bindparams(id=calculation_id)
        )
    await db.rollback()
