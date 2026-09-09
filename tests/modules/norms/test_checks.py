"""What blocks an application and what merely warns. Ruling 15 decides the split
3.6a deliberately left open: a fire ban blocks, a restriction warns."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.models import MediaFile
from app.modules.admin.models import Organization
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.gis.models import Contour, GisLayer
from app.modules.norms import checks
from app.modules.norms import params as norm_params
from app.modules.norms import service as norms_service
from app.modules.norms.calculator import CalcRequest, NormFact, ParamSnapshot
from app.modules.norms.models import ActivitySeason, Norm
from tests.modules.gis.conftest import (
    make_contour,
    make_feature,
    make_version,
    random_box_wkt,
    version_wkt,
)

pytestmark = pytest.mark.asyncio


async def _make_capacity_norm(
    db: AsyncSession,
    *,
    contour_id: uuid.UUID,
    activity_type_id: uuid.UUID,
    capacity: Decimal,
    gis_user: User,
    approval_doc: MediaFile,
) -> Norm:
    """A published norm carrying only a `capacity`, for a non-grazing
    activity — the direct-insert shape `conftest.py::published_grazing_norm`
    already uses for the grazing case, generalised so the seam-wiring tests
    below don't need their own `Norm` fixture."""
    norm = Norm(
        contour_id=contour_id,
        activity_type_id=activity_type_id,
        capacity=capacity,
        effective_from=date(2020, 1, 1),
        status="published",
        approval_doc_id=approval_doc.id,
        created_by=gis_user.id,
        approved_by=gis_user.id,
    )
    db.add(norm)
    await db.flush()
    return norm


SUMMER = {"windows": [{"from": "04-01", "to": "10-31"}]}
WINTER = {"windows": [{"from": "11-01", "to": "03-31"}]}
# Two windows with a real gap between them (June) — the case that justifies
# walking every day instead of just the two ends (fix round 1).
GAP = {"windows": [{"from": "04-01", "to": "05-31"}, {"from": "07-01", "to": "08-31"}]}


def _request(period_from: date, period_to: date, **over) -> CalcRequest:
    return CalcRequest(
        activity_code=over.pop("activity_code", "grazing"),
        on_date=date(2026, 8, 30),
        period_from=period_from,
        period_to=period_to,
        area_ha=Decimal("92"),
        items=over.pop("items", ()),
        quantity=over.pop("quantity", None),
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
        # `rounding_heads` is in every real snapshot (`params.BASE_CODES`), and
        # `_limit_check` needs it to round `remaining_sb` the way ruling 19
        # requires — an empty `values` here would be a shape production never
        # produces.
        values=over.pop("values", {"rounding_heads": {"mode": "floor"}}),
        tariffs=(),
        norm=norm,
        load_sb=over.pop("load_sb", Decimal("0")),
        load_source=over.pop("load_source", "none"),
        capacity_load=over.pop("capacity_load", Decimal("0")),
        capacity_load_source=over.pop("capacity_load_source", "none"),
        occupied_until=over.pop("occupied_until", None),
        occupied_until_source=over.pop("occupied_until_source", "none"),
        # `params.load_snapshot` fills this from `activity_types`; a test that
        # does not care leaves it unset, and the refusal then states no unit.
        quantity_unit=over.pop("quantity_unit", None),
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
    """Capacity 250, nothing committed, requested 300 → fail, and the details
    show why so an applicant can reduce the herd instead of guessing.
    Ruling #176 generalised the keys — `requested`/`capacity`/`remaining`,
    never grazing's old `used_sb`/`max_sb`/`remaining_sb` — so a front-end
    renders one shape for every activity."""
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
        "requested": "300",
        "capacity": "250",
        "committed": "0",
        "remaining": "250",
        "load_source": "none",
        "unit": "sb",
    }


async def test_the_limit_check_compares_against_the_floored_remainder(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """I1 (final review): MaxSB 250 with 0.5 conditional heads already
    committed leaves 249.5 — and ruling 19 says a limit is never rounded in
    the applicant's favour, so the comparison is against 249. A request for
    249.2 heads used to PASS against the unfloored remainder; it now fails,
    which is the direction the ruling names. `_limit_check` reads the same
    `calculator.remaining_sb` the amount does, so the two cannot drift."""
    request = _request(date(2026, 5, 1), date(2026, 9, 30))
    results = await checks.run_checks(
        db,
        request=request,
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(load_sb=Decimal("0.5")),
        used_sb=Decimal("249.2"),
    )
    limit = next(c for c in results if c["check"] == "limit")
    assert limit["details"]["remaining"] == "249"
    assert limit["result"] == "fail"


async def test_a_missing_norm_blocks_grazing_but_is_skipped_elsewhere(
    db: AsyncSession,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """Ruling 13: VMQ 689 imposes a feed-stock limit on grazing, and on nothing
    else. A haymaking request with no norm is normal, not an error — for the
    `norm` check specifically; the `limit` check's own answer for a
    capacity-less contour is covered separately (exclusivity, ruling #176)."""
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


async def test_the_season_check_catches_a_gap_between_two_windows(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """Fix round 1: two windows can each cover one END of a period while
    leaving a gap between them uncovered — 04-15 sits inside the first window,
    08-15 sits inside the second, but the whole of June is in neither. This is
    the case that justifies walking every day instead of just `period_from`/
    `period_to`; without it, "optimising" the walk back down to a
    boundaries-only check would still pass this test suite."""
    snapshot = _snapshot(
        norm=NormFact(
            id=uuid.uuid4(),
            yield_c_per_ha=Decimal("12"),
            max_sb=250,
            season=GAP,
            rotation={"rest_years": []},
        )
    )
    results = await checks.run_checks(
        db,
        request=_request(date(2026, 4, 15), date(2026, 8, 15)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=snapshot,
    )
    season_check = next(c for c in results if c["check"] == "season")
    assert season_check["result"] == "fail"
    assert season_check["details"]["reason"] == "outside_season"


async def test_a_reversed_period_is_refused_before_any_check_runs(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """`period_to < period_from` must not be allowed to quietly no-op every
    check to a false `pass` — most dangerously `features_intersecting`'s own
    validity predicate, which would drop a fire ban that genuinely covers the
    request out of its result set entirely."""
    with pytest.raises(DomainError) as raised:
        await checks.run_checks(
            db,
            request=_request(date(2026, 9, 30), date(2026, 5, 1)),
            contour_id=published_contour.id,
            activity_type_id=grazing_activity_id,
            snapshot=_snapshot(),
        )
    assert raised.value.code == "ERR-VAL-001"
    assert raised.value.details == {"reason": "period_reversed"}


async def test_a_period_longer_than_five_years_is_refused(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """VMQ 689's geobotanical survey is redone every five years — a
    norm-backed period cannot outlive the survey it is checked against, and
    the day-by-day season walk stays cheap as a result."""
    with pytest.raises(DomainError) as raised:
        await checks.run_checks(
            db,
            request=_request(date(2020, 1, 1), date(2027, 1, 1)),
            contour_id=published_contour.id,
            activity_type_id=grazing_activity_id,
            snapshot=_snapshot(),
        )
    assert raised.value.code == "ERR-VAL-001"
    assert raised.value.details == {"reason": "period_too_long"}


async def test_a_real_five_calendar_year_period_is_accepted(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """I10: `MAX_PERIOD_DAYS` was `5 * 365`, but a five-CALENDAR-year span is
    1826-1827 days — `2024-01-01 .. 2028-12-31` is `.days == 1826` — so the
    guard refused a lawful maximum period by a day or two. The bound is the
    survey's five-year life, not 1825 days."""
    assert (date(2028, 12, 31) - date(2024, 1, 1)).days == 1826
    results = await checks.run_checks(
        db,
        request=_request(date(2024, 1, 1), date(2028, 12, 31)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(),
    )
    # It gets past the guard and is judged on its merits (this one is out of
    # season, which is a CHECK result, not a refusal to look at all).
    assert {c["check"] for c in results} >= {"norm", "season", "rotation", "limit"}


async def test_a_rest_year_stored_as_a_string_still_blocks(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """I5's fail-open half, defended twice. `schemas.Rotation` refuses strings
    at the edge now, but `norms.rotation` is a JSONB column that was
    free-form until today — a row written before that validation existed must
    not silently pass for a resting year, so `_rotation_check` coerces
    instead of trusting."""
    results = await checks.run_checks(
        db,
        request=_request(date(2027, 5, 1), date(2027, 9, 30)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(
            norm=NormFact(
                id=uuid.uuid4(),
                yield_c_per_ha=Decimal("12"),
                max_sb=250,
                season=SUMMER,
                rotation={"rest_years": ["2027"]},
            )
        ),
    )
    rotation = next(c for c in results if c["check"] == "rotation")
    assert rotation["result"] == "fail"
    assert rotation["details"]["year"] == 2027


async def test_a_malformed_window_fails_closed_instead_of_raising(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """The other half of I5: `_in_window` used to do `window["from"]`, so a
    legacy row missing a bound raised a `KeyError` INSIDE the check — an
    uncaught 500 on a preview rather than a domain answer. A window that
    cannot be read does not cover the day, so the season blocks."""
    results = await checks.run_checks(
        db,
        request=_request(date(2026, 5, 1), date(2026, 6, 30)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(
            norm=NormFact(
                id=uuid.uuid4(),
                yield_c_per_ha=Decimal("12"),
                max_sb=250,
                season={"windows": [{"from": "04-01"}]},
                rotation={"rest_years": []},
            )
        ),
    )
    season = next(c for c in results if c["check"] == "season")
    assert season["result"] == "fail"


async def test_the_territory_checks_are_skipped_with_no_published_geometry(
    db: AsyncSession, draft_only_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """I6 (final review): `gis.repo.features_intersecting` inner-joins the
    contour's PUBLISHED version, so a contour whose geometry is still draft
    returns zero features — and `fire_ban` reported `pass`, a BLOCKING safety
    check asserting a fact it never tested. The state is reachable:
    `_build_request_and_snapshot` deliberately tolerates a contour with no
    published version, and a non-grazing activity needs no norm either.

    This is the anti-pattern `_season_check`'s own docstring articulates — "no
    windows configured is not the same as always in season" — applied
    consistently: nothing was verified, so nothing is asserted."""
    results = await checks.run_checks(
        db,
        request=_request(date(2026, 5, 1), date(2026, 9, 30)),
        contour_id=draft_only_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(),
    )
    fire_ban = next(c for c in results if c["check"] == "fire_ban")
    restrictions = next(c for c in results if c["check"] == "restrictions")
    assert fire_ban == {
        "check": "fire_ban",
        "result": "skipped",
        "details": {"reason": "no_published_geometry"},
    }
    assert restrictions["result"] == "skipped"
    assert restrictions["details"] == {"reason": "no_published_geometry"}
    # `skipped` is not `fail`, so it does not block on its own — but it can no
    # longer be mistaken for a verified `pass` either.
    assert checks.is_blocked(results) is False


# --- Ruling #176 (stage 9): capacity generalises beyond grazing, and a ------
# capacity-less contour is EXCLUSIVE rather than unlimited. The tests above
# this line pin the pre-existing grazing behaviour (still passing, renamed
# details); everything below is new.


async def test_a_haymaking_request_over_the_remainder_is_refused(
    db: AsyncSession, published_contour: Contour, haymaking_activity_id: uuid.UUID
) -> None:
    """Capacity 10 ha, 3 already committed, 8 requested → fail: the same
    `requested ≤ capacity − committed` rule grazing already had, now working
    for an activity that was never checked at all before this stage."""
    request = _request(
        date(2026, 5, 1), date(2026, 9, 30), activity_code="haymaking", quantity=Decimal("8")
    )
    snapshot = _snapshot(
        norm=NormFact(
            id=uuid.uuid4(),
            yield_c_per_ha=None,
            max_sb=None,
            season=None,
            rotation=None,
            capacity=Decimal("10"),
        ),
        capacity_load=Decimal("3"),
        capacity_load_source="permits",
        quantity_unit="ha",
    )
    results = await checks.run_checks(
        db,
        request=request,
        contour_id=published_contour.id,
        activity_type_id=haymaking_activity_id,
        snapshot=snapshot,
    )
    limit = next(c for c in results if c["check"] == "limit")
    assert limit["result"] == "fail"
    assert limit["details"] == {
        "requested": "8",
        "capacity": "10",
        "committed": "3",
        "remaining": "7",
        "load_source": "permits",
        "unit": "ha",
    }
    assert checks.is_blocked(results) is True


async def test_a_haymaking_request_within_the_remainder_passes(
    db: AsyncSession, published_contour: Contour, haymaking_activity_id: uuid.UUID
) -> None:
    """Same capacity and committed load as above, but 5 requested instead of
    8 — within the 7 ha remaining, so it passes."""
    request = _request(
        date(2026, 5, 1), date(2026, 9, 30), activity_code="haymaking", quantity=Decimal("5")
    )
    snapshot = _snapshot(
        norm=NormFact(
            id=uuid.uuid4(),
            yield_c_per_ha=None,
            max_sb=None,
            season=None,
            rotation=None,
            capacity=Decimal("10"),
        ),
        capacity_load=Decimal("3"),
        capacity_load_source="permits",
        quantity_unit="ha",
    )
    results = await checks.run_checks(
        db,
        request=request,
        contour_id=published_contour.id,
        activity_type_id=haymaking_activity_id,
        snapshot=snapshot,
    )
    limit = next(c for c in results if c["check"] == "limit")
    assert limit["result"] == "pass"
    assert limit["details"]["remaining"] == "7"
    assert checks.is_blocked(results) is False


async def test_a_declared_quantity_needs_no_pricing_to_be_checked(
    db: AsyncSession, published_contour: Contour, haymaking_activity_id: uuid.UUID
) -> None:
    """Unlike grazing's `used_sb` (a priced fact), a non-grazing `quantity` is
    on the request itself — the reviewer-facing path (`used_sb=None`, the
    caller never prices the request) still gets a REAL comparison here,
    where grazing's own unpriced screen reports `skipped`/`not_computed`."""
    request = _request(
        date(2026, 5, 1), date(2026, 9, 30), activity_code="haymaking", quantity=Decimal("8")
    )
    snapshot = _snapshot(
        norm=NormFact(
            id=uuid.uuid4(),
            yield_c_per_ha=None,
            max_sb=None,
            season=None,
            rotation=None,
            capacity=Decimal("10"),
        ),
    )
    results = await checks.run_checks(
        db,
        request=request,
        contour_id=published_contour.id,
        activity_type_id=haymaking_activity_id,
        snapshot=snapshot,
        used_sb=None,  # the reviewer-facing "no money" path
    )
    limit = next(c for c in results if c["check"] == "limit")
    assert limit["result"] == "pass"
    assert limit["details"]["requested"] == "8"


async def test_a_missing_quantity_is_skipped_not_a_manufactured_zero(
    db: AsyncSession, published_contour: Contour, haymaking_activity_id: uuid.UUID
) -> None:
    """A capacity exists, but nothing was declared to compare against it —
    `skipped`/`not_computed`, the same honest answer grazing's own unpriced
    path gives, never a request read as zero."""
    request = _request(date(2026, 5, 1), date(2026, 9, 30), activity_code="haymaking")
    snapshot = _snapshot(
        norm=NormFact(
            id=uuid.uuid4(),
            yield_c_per_ha=None,
            max_sb=None,
            season=None,
            rotation=None,
            capacity=Decimal("10"),
        ),
    )
    results = await checks.run_checks(
        db,
        request=request,
        contour_id=published_contour.id,
        activity_type_id=haymaking_activity_id,
        snapshot=snapshot,
    )
    limit = next(c for c in results if c["check"] == "limit")
    assert limit == {"check": "limit", "result": "skipped", "details": {"reason": "not_computed"}}


async def test_no_capacity_with_no_registered_provider_is_skipped_not_assumed_free(
    db: AsyncSession, published_contour: Contour, haymaking_activity_id: uuid.UUID
) -> None:
    """No norm at all → no capacity → EXCLUSIVE branch, but
    `EXCLUSIVITY_PROVIDERS` is empty (T6 wires it in the next wave) — the
    honest answer is `skipped`, never a manufactured "free"."""
    request = _request(date(2026, 5, 1), date(2026, 9, 30), activity_code="haymaking")
    snapshot = _snapshot(norm=None)
    results = await checks.run_checks(
        db,
        request=request,
        contour_id=published_contour.id,
        activity_type_id=haymaking_activity_id,
        snapshot=snapshot,
    )
    limit = next(c for c in results if c["check"] == "limit")
    assert limit == {
        "check": "limit",
        "result": "skipped",
        "details": {"reason": "no_occupancy_provider"},
    }
    assert checks.is_blocked(results) is False


async def test_a_capacity_less_contour_admits_one_active_permit_and_refuses_the_second(
    db: AsyncSession,
    published_contour: Contour,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """Ruling #176, Oybek's option а, exercised through the REAL seam
    (`norm_params.load_snapshot` → `service.EXCLUSIVITY_PROVIDERS` →
    `checks.run_checks`), not a synthetic snapshot: with nothing overlapping,
    the request passes; with an ACTIVE permit already covering the period,
    the very same request is refused and told the day it frees up. T4 owns
    the seam and this proof of its behaviour; T6 registers the real query
    against `permits` in the next wave — this is what that registration must
    satisfy."""
    request = _request(
        date(2026, 5, 1), date(2026, 9, 30), activity_code="haymaking", quantity=Decimal("2")
    )

    async def free(
        db_: AsyncSession,
        contour_id: uuid.UUID,
        activity_type_id: uuid.UUID,
        period_from: date,
        period_to: date,
    ) -> date | None:
        return None

    norms_service.EXCLUSIVITY_PROVIDERS.append(free)
    try:
        snapshot = await norm_params.load_snapshot(
            db,
            request=request,
            contour_id=published_contour.id,
            activity_type_id=haymaking_activity_id,
        )
        results = await checks.run_checks(
            db,
            request=request,
            contour_id=published_contour.id,
            activity_type_id=haymaking_activity_id,
            snapshot=snapshot,
        )
        limit = next(c for c in results if c["check"] == "limit")
        assert limit == {
            "check": "limit",
            "result": "pass",
            "details": {"reason": "exclusive_available"},
        }
    finally:
        norms_service.EXCLUSIVITY_PROVIDERS.remove(free)

    async def occupied(
        db_: AsyncSession,
        contour_id: uuid.UUID,
        activity_type_id: uuid.UUID,
        period_from: date,
        period_to: date,
    ) -> date | None:
        return date(2026, 9, 30)

    norms_service.EXCLUSIVITY_PROVIDERS.append(occupied)
    try:
        snapshot = await norm_params.load_snapshot(
            db,
            request=request,
            contour_id=published_contour.id,
            activity_type_id=haymaking_activity_id,
        )
        results = await checks.run_checks(
            db,
            request=request,
            contour_id=published_contour.id,
            activity_type_id=haymaking_activity_id,
            snapshot=snapshot,
        )
        limit = next(c for c in results if c["check"] == "limit")
        assert limit == {
            "check": "limit",
            "result": "fail",
            "details": {"reason": "exclusive_occupied", "occupied_until": "2026-09-30"},
        }
        assert checks.is_blocked(results) is True
    finally:
        norms_service.EXCLUSIVITY_PROVIDERS.remove(occupied)


async def test_a_null_max_sb_for_grazing_is_now_exclusive_not_skipped(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """Ruling #176 deliberately changes grazing too: a norm with a null
    `max_sb` used to report `skipped`/`no_limit` here — silently unlimited.
    It now lands in the same exclusive branch as every other capacity-less
    activity, through the real seam."""
    request = _request(date(2026, 5, 1), date(2026, 9, 30), activity_code="grazing")
    snapshot_no_provider = await norm_params.load_snapshot(
        db, request=request, contour_id=published_contour.id, activity_type_id=grazing_activity_id
    )
    # No norm at all on `published_contour` for grazing (no fixture inserted
    # one), so `resolve_capacity` already reads `max_sb=None` off nothing —
    # the null-max_sb case and the no-norm-at-all case share this one branch
    # by construction (`calculator.resolve_capacity`'s own contract).
    results = await checks.run_checks(
        db,
        request=request,
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=snapshot_no_provider,
        used_sb=Decimal("10"),
    )
    limit = next(c for c in results if c["check"] == "limit")
    assert limit["result"] == "skipped"
    assert limit["details"] == {"reason": "no_occupancy_provider"}

    async def occupied(
        db_: AsyncSession,
        contour_id: uuid.UUID,
        activity_type_id: uuid.UUID,
        period_from: date,
        period_to: date,
    ) -> date | None:
        return date(2026, 12, 31)

    norms_service.EXCLUSIVITY_PROVIDERS.append(occupied)
    try:
        snapshot = await norm_params.load_snapshot(
            db,
            request=request,
            contour_id=published_contour.id,
            activity_type_id=grazing_activity_id,
        )
        results = await checks.run_checks(
            db,
            request=request,
            contour_id=published_contour.id,
            activity_type_id=grazing_activity_id,
            snapshot=snapshot,
            used_sb=Decimal("10"),
        )
        limit = next(c for c in results if c["check"] == "limit")
        assert limit["result"] == "fail"
        assert limit["details"]["reason"] == "exclusive_occupied"
        assert limit["details"]["occupied_until"] == "2026-12-31"
    finally:
        norms_service.EXCLUSIVITY_PROVIDERS.remove(occupied)


async def test_the_capacity_load_seam_reports_none_when_empty(
    db: AsyncSession, published_contour: Contour, haymaking_activity_id: uuid.UUID
) -> None:
    """`CAPACITY_LOAD_PROVIDERS` is empty until T6 registers a provider (the
    same placeholder `LOAD_PROVIDERS` carried until 3.11) — the committed
    quantity is reported as zero, but honestly labelled `"none"`, never
    mistaken for a real measurement."""
    committed, source = await norms_service.committed_capacity_load(
        db, published_contour.id, haymaking_activity_id, date(2026, 5, 1), date(2026, 9, 30)
    )
    assert (committed, source) == (Decimal("0"), "none")


async def test_the_capacity_load_seam_sums_every_registered_provider(
    db: AsyncSession,
    published_contour: Contour,
    haymaking_activity_id: uuid.UUID,
    gis_user: User,
    approval_doc: MediaFile,
) -> None:
    """Two providers, summed, over the REAL seam this time (not a synthetic
    snapshot): `norm_params.load_snapshot` calls `committed_capacity_load`
    for a haymaking norm with a `capacity` set, exactly as `LOAD_PROVIDERS`
    is summed for grazing's `load_sb`."""
    await _make_capacity_norm(
        db,
        contour_id=published_contour.id,
        activity_type_id=haymaking_activity_id,
        capacity=Decimal("10"),
        gis_user=gis_user,
        approval_doc=approval_doc,
    )

    async def first(
        db_: AsyncSession,
        contour_id: uuid.UUID,
        activity_type_id: uuid.UUID,
        period_from: date,
        period_to: date,
    ) -> Decimal:
        return Decimal("3")

    async def second(
        db_: AsyncSession,
        contour_id: uuid.UUID,
        activity_type_id: uuid.UUID,
        period_from: date,
        period_to: date,
    ) -> Decimal:
        return Decimal("2")

    norms_service.CAPACITY_LOAD_PROVIDERS.extend([first, second])
    try:
        request = _request(
            date(2026, 5, 1), date(2026, 9, 30), activity_code="haymaking", quantity=Decimal("6")
        )
        snapshot = await norm_params.load_snapshot(
            db,
            request=request,
            contour_id=published_contour.id,
            activity_type_id=haymaking_activity_id,
        )
        assert snapshot.capacity_load == Decimal("5")
        assert snapshot.capacity_load_source == "permits"
        results = await checks.run_checks(
            db,
            request=request,
            contour_id=published_contour.id,
            activity_type_id=haymaking_activity_id,
            snapshot=snapshot,
        )
        limit = next(c for c in results if c["check"] == "limit")
        # 6 requested > 10 − 5 = 5 remaining → refused.
        assert limit["result"] == "fail"
        # `capacity` round-trips out of Postgres at the column's own scale —
        # `numeric(14,4)` — not the caller's unpadded "10" (the same
        # fixed-scale-NUMERIC lesson `TariffOut.coefficient`/`NormOut.
        # yield_c_per_ha` already observe); `remaining` inherits that scale
        # from the subtraction. `requested`/`committed` are Python-only
        # figures here (the request body, the stub providers' sum) and stay
        # at the precision they were written with.
        assert limit["details"] == {
            "requested": "6",
            "capacity": "10.0000",
            "committed": "5",
            "remaining": "5.0000",
            "load_source": "permits",
            "unit": "ha",
        }
    finally:
        norms_service.CAPACITY_LOAD_PROVIDERS.remove(first)
        norms_service.CAPACITY_LOAD_PROVIDERS.remove(second)


# --- Ruling #177 (stage 9): the leshoz x activity dictionary's resolution ---
# order — a contour's own norm windows override; the leshoz's
# `activity_seasons` row is the fallback; neither is unchanged today's
# meaning. `_min_term_check` and the fail-closed malformed-window property
# are covered here too; the dictionary's own CRUD/zone surface is
# `test_activity_seasons_api.py`'s territory.


async def _make_activity_season(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    activity_type_id: uuid.UUID,
    season: dict,
    min_term_days: int | None,
    created_by: uuid.UUID,
) -> ActivitySeason:
    row = ActivitySeason(
        organization_id=organization_id,
        activity_type_id=activity_type_id,
        season=season,
        min_term_days=min_term_days,
        created_by=created_by,
    )
    db.add(row)
    await db.flush()
    return row


async def test_the_dictionary_is_used_when_the_contour_has_no_norm_at_all(
    db: AsyncSession,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    leshoz: Organization,
    gis_user: User,
) -> None:
    """The whole point of ruling #177: a leshoz with no geobotanical survey,
    and therefore no `Norm`, still states its season once and has it
    enforced — today's behaviour (`_snapshot(norm=None)`) used to answer
    `skipped`/`no_norm` unconditionally here."""
    await _make_activity_season(
        db,
        organization_id=leshoz.id,
        activity_type_id=grazing_activity_id,
        season=SUMMER,
        min_term_days=None,
        created_by=gis_user.id,
    )
    results = await checks.run_checks(
        db,
        request=_request(date(2026, 5, 1), date(2026, 9, 30)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(norm=None),
    )
    season_check = next(c for c in results if c["check"] == "season")
    assert season_check["result"] == "pass"
    assert season_check["details"]["source"] == "activity_season"

    outside = await checks.run_checks(
        db,
        request=_request(date(2026, 1, 1), date(2026, 2, 28)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(norm=None),
    )
    outside_season = next(c for c in outside if c["check"] == "season")
    assert outside_season["result"] == "fail"
    assert outside_season["details"]["reason"] == "outside_season"
    assert outside_season["details"]["source"] == "activity_season"


async def test_one_dictionary_row_covers_every_contour_of_the_leshoz(
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
    gis_user: User,
    approval_doc: MediaFile,
) -> None:
    """Acceptance criterion: a leshoz states its season ONCE and every one
    of its contours inherits it — no per-contour norm at all."""
    await _make_activity_season(
        db,
        organization_id=leshoz.id,
        activity_type_id=grazing_activity_id,
        season=SUMMER,
        min_term_days=None,
        created_by=gis_user.id,
    )
    for _ in range(2):
        contour = await make_contour(db, contours_layer, leshoz)
        await make_version(
            db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
        )
        await db.flush()
        results = await checks.run_checks(
            db,
            request=_request(date(2026, 5, 1), date(2026, 9, 30)),
            contour_id=contour.id,
            activity_type_id=grazing_activity_id,
            snapshot=_snapshot(norm=None),
        )
        season_check = next(c for c in results if c["check"] == "season")
        assert season_check["result"] == "pass"
        assert season_check["details"]["source"] == "activity_season"


async def test_a_norms_own_windows_override_the_dictionary(
    db: AsyncSession,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    leshoz: Organization,
    gis_user: User,
) -> None:
    """The dictionary states WINTER for the whole leshoz; this ONE contour's
    norm overrides it with SUMMER — a May-September request must pass
    against the norm's own windows, not fail against the dictionary's."""
    await _make_activity_season(
        db,
        organization_id=leshoz.id,
        activity_type_id=grazing_activity_id,
        season=WINTER,
        min_term_days=None,
        created_by=gis_user.id,
    )
    snapshot = _snapshot(
        norm=NormFact(
            id=uuid.uuid4(),
            yield_c_per_ha=Decimal("12"),
            max_sb=250,
            season=SUMMER,
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
    season_check = next(c for c in results if c["check"] == "season")
    assert season_check["result"] == "pass"
    assert season_check["details"]["source"] == "norm"


async def test_neither_norm_nor_dictionary_is_still_skipped_no_season_defined(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    """Today's exact meaning, unchanged (ruling #177's own wording): neither
    source present is `skipped`/`no_season_defined`, never a pass or a fail."""
    results = await checks.run_checks(
        db,
        request=_request(date(2026, 5, 1), date(2026, 9, 30)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(norm=None),
    )
    season_check = next(c for c in results if c["check"] == "season")
    assert season_check == {
        "check": "season",
        "result": "skipped",
        "details": {"reason": "no_season_defined"},
    }


async def test_a_malformed_dictionary_window_fails_closed_not_crashes(
    db: AsyncSession,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    leshoz: Organization,
    gis_user: User,
) -> None:
    """`checks._season_check`'s docstring (via `_in_window`) says an
    unreadable window blocks rather than passing — this is that same
    property, reached through the NEW dictionary source rather than a
    pre-existing `Norm` row. A row written outside the API's own
    `schemas.Season` validation (raw ORM, exactly like a pre-stage-9 `Norm`
    row could already hold) must not crash the check."""
    await _make_activity_season(
        db,
        organization_id=leshoz.id,
        activity_type_id=grazing_activity_id,
        season={"windows": [{"nonsense": True}]},
        min_term_days=None,
        created_by=gis_user.id,
    )
    results = await checks.run_checks(
        db,
        request=_request(date(2026, 5, 1), date(2026, 9, 30)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(norm=None),
    )
    season_check = next(c for c in results if c["check"] == "season")
    assert season_check["result"] == "fail"
    assert season_check["details"]["reason"] == "outside_season"


async def test_a_period_shorter_than_the_minimum_term_is_refused_by_name(
    db: AsyncSession,
    published_contour: Contour,
    haymaking_activity_id: uuid.UUID,
    leshoz: Organization,
    gis_user: User,
) -> None:
    """Haymaking, not grazing: grazing's own `norm` check would block FIRST
    (no norm at all here) and `first_blocking_error` reports the first
    blocking failure in list order — haymaking's `norm` check is `skipped`
    (`not_required_for_activity`), so `min_term` is the only one that can
    block, proving `ERR-NORM-003` is really this check's own code."""
    await _make_activity_season(
        db,
        organization_id=leshoz.id,
        activity_type_id=haymaking_activity_id,
        season=SUMMER,
        min_term_days=30,
        created_by=gis_user.id,
    )
    # 10 days, inclusive of both ends — well under the 30-day minimum.
    results = await checks.run_checks(
        db,
        request=_request(
            date(2026, 5, 1), date(2026, 5, 10), activity_code="haymaking", quantity=Decimal("1")
        ),
        contour_id=published_contour.id,
        activity_type_id=haymaking_activity_id,
        snapshot=_snapshot(norm=None),
    )
    min_term = next(c for c in results if c["check"] == "min_term")
    assert min_term["result"] == "fail"
    assert min_term["details"] == {
        "reason": "period_too_short",
        "min_term_days": 30,
        "requested_days": 10,
    }
    assert checks.is_blocked(results) is True
    blocking_error = checks.first_blocking_error(results)
    assert blocking_error is not None
    assert blocking_error.code == "ERR-NORM-003"


async def test_a_period_meeting_the_minimum_term_passes(
    db: AsyncSession,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    leshoz: Organization,
    gis_user: User,
) -> None:
    await _make_activity_season(
        db,
        organization_id=leshoz.id,
        activity_type_id=grazing_activity_id,
        season=SUMMER,
        min_term_days=30,
        created_by=gis_user.id,
    )
    # 2026-05-01 .. 2026-05-30 is exactly 30 days inclusive.
    results = await checks.run_checks(
        db,
        request=_request(date(2026, 5, 1), date(2026, 5, 30)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(norm=None),
    )
    min_term = next(c for c in results if c["check"] == "min_term")
    assert min_term == {
        "check": "min_term",
        "result": "pass",
        "details": {"min_term_days": 30},
    }


async def test_no_dictionary_row_means_min_term_is_skipped_not_zero(
    db: AsyncSession, published_contour: Contour, grazing_activity_id: uuid.UUID
) -> None:
    results = await checks.run_checks(
        db,
        request=_request(date(2026, 5, 1), date(2026, 5, 1)),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        snapshot=_snapshot(norm=None),
    )
    min_term = next(c for c in results if c["check"] == "min_term")
    assert min_term == {
        "check": "min_term",
        "result": "skipped",
        "details": {"reason": "no_min_term_defined"},
    }


async def test_resolve_effective_windows_prefers_the_norm_then_the_dictionary_then_none() -> None:
    """The one function both `_season_check` and `service.effective_season`
    (the wizard's public read) call — a direct unit test on top of the
    end-to-end ones above."""
    assert checks.resolve_effective_windows(SUMMER, WINTER) == (SUMMER["windows"], "norm")
    assert checks.resolve_effective_windows(None, WINTER) == (WINTER["windows"], "activity_season")
    assert checks.resolve_effective_windows({"windows": []}, WINTER) == (
        WINTER["windows"],
        "activity_season",
    )
    assert checks.resolve_effective_windows(None, None) == ([], "none")
    assert checks.resolve_effective_windows(
        "garbage",  # type: ignore[arg-type]
        "also garbage",  # type: ignore[arg-type]
    ) == ([], "none")
