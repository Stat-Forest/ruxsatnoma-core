"""What blocks an application and what merely warns. Ruling 15 decides the split
3.6a deliberately left open: a fire ban blocks, a restriction warns."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.gis import service as gis_service
from app.modules.gis.models import Contour, GisLayer
from app.modules.norms import checks
from app.modules.norms.calculator import CalcRequest, NormFact, ParamSnapshot
from tests.modules.gis.conftest import make_feature, version_wkt

pytestmark = pytest.mark.asyncio

SUMMER = {"windows": [{"from": "04-01", "to": "10-31"}]}
WINTER = {"windows": [{"from": "11-01", "to": "03-31"}]}


def _request(period_from: date, period_to: date, **over) -> CalcRequest:
    return CalcRequest(
        activity_code=over.pop("activity_code", "grazing"),
        on_date=date(2026, 8, 30),
        period_from=period_from,
        period_to=period_to,
        area_ha=Decimal("92"),
        items=(),
        quantity=None,
        benefit_code=None,
        **over,
    )


def _snapshot(**over) -> ParamSnapshot:
    norm = over.pop(
        "norm",
        NormFact(
            id=uuid.uuid4(),
            yield_c_per_ha=Decimal("12"),
            max_sb=250,
            season=SUMMER,
            rotation={"rest_years": []},
        ),
    )
    return ParamSnapshot(
        values=over.pop("values", {}),
        tariffs=(),
        norm=norm,
        load_sb=over.pop("load_sb", Decimal("0")),
        load_source="none",
    )


@pytest.mark.parametrize(
    ("season", "period_from", "period_to", "expected"),
    [
        (SUMMER, date(2026, 5, 1), date(2026, 9, 30), "pass"),
        (SUMMER, date(2026, 3, 1), date(2026, 9, 30), "fail"),  # starts too early
        (SUMMER, date(2026, 5, 1), date(2026, 11, 30), "fail"),  # ends too late
        # A winter window wraps the new year: `from` > `to` as strings, and a
        # naive `from <= day <= to` comparison would reject the whole period.
        (WINTER, date(2026, 12, 1), date(2027, 2, 28), "pass"),
        (WINTER, date(2026, 5, 1), date(2026, 6, 30), "fail"),
    ],
)
async def test_the_season_window_is_checked_at_both_ends(
    db: AsyncSession,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    season: dict,
    period_from: date,
    period_to: date,
    expected: str,
) -> None:
    snapshot = _snapshot(
        norm=NormFact(
            id=uuid.uuid4(),
            yield_c_per_ha=Decimal("12"),
            max_sb=250,
            season=season,
            rotation={"rest_years": []},
        )
    )
    results = await checks.run_checks(
        db,
        request=_request(period_from, period_to),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=snapshot,
    )
    season_check = next(c for c in results if c["check"] == "season")
    assert season_check["result"] == expected
    if expected == "fail":
        assert season_check["details"]["reason"] == "outside_season"


async def test_a_rest_year_fails_the_rotation_check(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """VMQ 689's rotation exists to let a plot recover; grazing it during its
    rest year is exactly what the norm forbids."""
    snapshot = _snapshot(
        norm=NormFact(
            id=uuid.uuid4(),
            yield_c_per_ha=Decimal("12"),
            max_sb=250,
            season=SUMMER,
            rotation={"rest_years": [2027]},
        )
    )
    results = await checks.run_checks(
        db,
        request=_request(date(2027, 5, 1), date(2027, 9, 30)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=snapshot,
    )
    rotation = next(c for c in results if c["check"] == "rotation")
    assert rotation["result"] == "fail"
    assert rotation["details"] == {"reason": "rest_year", "year": 2027}


async def test_a_fire_ban_overlapping_the_period_blocks(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    layer = (await db.execute(select(GisLayer).where(GisLayer.code == "fire_bans"))).scalar_one()
    version = await gis_service.published_version(db, published_contour.id)
    assert version is not None
    version_geometry = await version_wkt(db, version)
    await make_feature(
        db,
        layer,
        version_geometry,
        valid_from=date(2026, 6, 1),
        valid_to=date(2026, 8, 31),
        status="published",
    )
    await db.flush()
    results = await checks.run_checks(
        db,
        request=_request(date(2026, 5, 1), date(2026, 9, 30)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(),
    )
    fire_ban = next(c for c in results if c["check"] == "fire_ban")
    assert fire_ban["result"] == "fail"
    assert checks.is_blocked(results) is True


async def test_a_fire_ban_that_expired_before_the_period_does_not_block(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    layer = (await db.execute(select(GisLayer).where(GisLayer.code == "fire_bans"))).scalar_one()
    version = await gis_service.published_version(db, published_contour.id)
    assert version is not None
    await make_feature(
        db,
        layer,
        await version_wkt(db, version),
        valid_from=date(2025, 6, 1),
        valid_to=date(2025, 8, 31),
        status="published",
    )
    await db.flush()
    results = await checks.run_checks(
        db,
        request=_request(date(2026, 5, 1), date(2026, 9, 30)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(),
    )
    assert next(c for c in results if c["check"] == "fire_ban")["result"] == "pass"


async def test_a_restriction_warns_but_does_not_block(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """Ruling 15: grazing on a restricted plot is limited, not impossible — the
    reviewer of 3.9 weighs it, the engine does not refuse it."""
    layer = (await db.execute(select(GisLayer).where(GisLayer.code == "restrictions"))).scalar_one()
    version = await gis_service.published_version(db, published_contour.id)
    assert version is not None
    await make_feature(db, layer, await version_wkt(db, version), status="published")
    await db.flush()
    results = await checks.run_checks(
        db,
        request=_request(date(2026, 5, 1), date(2026, 9, 30)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(),
    )
    assert next(c for c in results if c["check"] == "restrictions")["result"] == "warning"
    assert checks.is_blocked(results) is False


async def test_the_limit_check_reports_all_three_numbers(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """MaxSB 250, nothing committed, UsedSB 300 → fail, and the details show why
    so an applicant can reduce the herd instead of guessing."""
    request = _request(date(2026, 5, 1), date(2026, 9, 30))
    results = await checks.run_checks(
        db,
        request=request,
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(),
        used_sb=Decimal("300"),
    )
    limit = next(c for c in results if c["check"] == "limit")
    assert limit["result"] == "fail"
    assert limit["details"] == {
        "used_sb": "300",
        "max_sb": 250,
        "committed_sb": "0",
        "remaining_sb": "250",
        "load_source": "none",
    }


async def test_a_missing_norm_blocks_grazing_but_is_skipped_elsewhere(
    db: AsyncSession,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """Ruling 13: VMQ 689 imposes a feed-stock limit on grazing, and on nothing
    else. A haymaking request with no norm is normal, not an error."""
    grazing = await checks.run_checks(
        db,
        request=_request(date(2026, 5, 1), date(2026, 9, 30)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(norm=None),
    )
    assert next(c for c in grazing if c["check"] == "norm")["result"] == "fail"

    haymaking = await checks.run_checks(
        db,
        request=_request(date(2026, 5, 1), date(2026, 9, 30), activity_code="haymaking"),
        contour_id=published_contour.id,
        activity_type_id=haymaking_activity_id,
        snapshot=_snapshot(norm=None),
    )
    norm_check = next(c for c in haymaking if c["check"] == "norm")
    assert norm_check["result"] == "skipped"
    assert norm_check["details"]["reason"] == "not_required_for_activity"


async def test_a_norm_with_no_season_recorded_is_skipped_not_passed(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """`norms.season` is a nullable column — a norm can be published without
    ever recording one. The check must not silently claim a period was
    verified against a season nobody defined."""
    snapshot = _snapshot(
        norm=NormFact(
            id=uuid.uuid4(),
            yield_c_per_ha=Decimal("12"),
            max_sb=250,
            season=None,
            rotation={"rest_years": []},
        )
    )
    results = await checks.run_checks(
        db,
        request=_request(date(2026, 5, 1), date(2026, 9, 30)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=snapshot,
    )
    assert next(c for c in results if c["check"] == "season")["result"] == "skipped"


async def test_a_norm_with_no_rotation_recorded_passes(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """Unlike an unrecorded season, no `rotation` at all means no rest years —
    a confident pass, not an unanswered question."""
    snapshot = _snapshot(
        norm=NormFact(
            id=uuid.uuid4(), yield_c_per_ha=Decimal("12"), max_sb=250, season=SUMMER, rotation=None
        )
    )
    results = await checks.run_checks(
        db,
        request=_request(date(2026, 5, 1), date(2026, 9, 30)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=snapshot,
    )
    assert next(c for c in results if c["check"] == "rotation")["result"] == "pass"
