"""What migration 0012 puts in the database — the numbers themselves, read from
the primary sources on 2026-08-30 (VMQ 278 annex, VMQ 689). These assertions are
the regression test for a wrong tariff, which is the most expensive kind of bug
this stage can ship."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("activity", "group", "coefficient", "unit"),
    [
        ("grazing", "large_adult", Decimal("0.450000"), "head"),
        ("grazing", "large_young", Decimal("0.150000"), "head"),
        ("grazing", "small_adult", Decimal("0.100000"), "head"),
        ("grazing", "small_young", Decimal("0.030000"), "head"),
        ("haymaking", None, Decimal("1.500000"), "ha"),
        ("apiary", None, Decimal("0.050000"), "hive"),
        ("deadwood", None, Decimal("0.400000"), "m3"),
        ("recreation", None, Decimal("0.050000"), "person_day"),
    ],
)
async def test_vmq_278_rates_are_seeded(
    db: AsyncSession, activity: str, group: str | None, coefficient: Decimal, unit: str
) -> None:
    row = await db.execute(
        text(
            "SELECT t.coefficient, t.quantity_unit, t.status, t.basis FROM tariffs t "
            "JOIN activity_types a ON a.id = t.activity_type_id "
            "WHERE a.code = :activity AND t.livestock_group IS NOT DISTINCT FROM :group"
        ).bindparams(activity=activity, group=group)
    )
    coefficient_db, unit_db, status, basis = row.one()
    assert coefficient_db == coefficient
    assert unit_db == unit
    assert status == "published"
    assert "278" in basis


async def test_science_has_no_tariff(db: AsyncSession) -> None:
    """VMQ 278's annex has no rate for scientific research — it is contracted
    separately (ruling 1). Seeding a zero-coefficient row would look like a
    decision; seeding nothing is the fact."""
    row = await db.execute(
        text(
            "SELECT count(*) FROM tariffs t JOIN activity_types a ON a.id = t.activity_type_id "
            "WHERE a.code = 'science'"
        )
    )
    assert row.scalar_one() == 0


async def test_bhm_has_both_dated_values(db: AsyncSession) -> None:
    """Ruling 7: 412 000 sum until 2026-08-31, 440 000 from 2026-09-01."""
    rows = await db.execute(
        text(
            "SELECT value #>> '{}', effective_from, effective_to FROM rule_parameters "
            "WHERE code = 'bhm' AND status = 'published' ORDER BY effective_from"
        )
    )
    assert rows.all() == [
        ("412000", date(2025, 8, 1), date(2026, 8, 31)),
        ("440000", date(2026, 9, 1), None),
    ]


@pytest.mark.parametrize(
    ("code", "value"),
    [("safety_reserve", "0.85"), ("sb_feed_norm", "3.74"), ("season_share", "1.0")],
)
async def test_vmq_689_constants_are_published(db: AsyncSession, code: str, value: str) -> None:
    row = await db.execute(
        text(
            "SELECT value #>> '{}' FROM rule_parameters WHERE code = :code AND status = 'published'"
        ).bindparams(code=code)
    )
    assert row.scalar_one() == value


async def test_conditional_head_coefficients_are_seeded_as_drafts(db: AsyncSession) -> None:
    """Ruling 8: VMQ 689 annex 5 has not arrived, so the ten coefficients ship as
    drafts with a basis that says so. The calculator only ever reads published
    rows, so a grazing calculation fails loudly until someone publishes them."""
    rows = await db.execute(
        text(
            "SELECT code, status, basis FROM rule_parameters WHERE code LIKE 'coef_sb:%' "
            "ORDER BY code"
        )
    )
    seeded = rows.all()
    assert len(seeded) == 10
    assert {status for _, status, _ in seeded} == {"draft"}
    assert all("provisional" in basis for _, _, basis in seeded)


async def test_every_livestock_type_maps_to_a_tariff_group(db: AsyncSession) -> None:
    """Ruling 9: without the mapping a grazing fee cannot be computed at all, so
    the seed must cover every livestock type the reference data knows."""
    rows = await db.execute(
        text(
            "SELECT l.code, p.value #>> '{}' FROM livestock_types l "
            "LEFT JOIN rule_parameters p "
            "  ON p.code = 'tariff_group:' || l.code AND p.status = 'published'"
        )
    )
    # A dict comprehension, not dict(rows.all()): a raw text() query returns
    # Row[Any], and pyright's dict() overloads cannot confirm an untyped Row
    # is a 2-tuple, so they resolve to the wrong overload (reportCallIssue).
    mapping = {row[0]: row[1] for row in rows.all()}
    assert len(mapping) == 10
    assert None not in mapping.values()
    assert set(mapping.values()) <= {"large_adult", "large_young", "small_adult", "small_young"}


async def test_the_three_wrong_activity_units_are_corrected(db: AsyncSession) -> None:
    """Ruling 2 — the 3.3a seed guessed these before the annex was read."""
    rows = await db.execute(
        text(
            "SELECT code, quantity_unit FROM activity_types WHERE code IN "
            "('haymaking', 'recreation', 'deadwood')"
        )
    )
    corrected = {row[0]: row[1] for row in rows.all()}
    assert corrected == {"haymaking": "ha", "recreation": "person_day", "deadwood": "m3"}
