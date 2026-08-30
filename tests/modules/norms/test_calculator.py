"""The arithmetic, in isolation. Every number here is traceable to a primary
source: VMQ 689 for the limit (0.85, 3.74), VMQ 278 for the rates, and the real
BHM (412 000 sum until 2026-08-31, 440 000 from 2026-09-01)."""

from datetime import date
from decimal import Decimal

import pytest

from app.core.errors import DomainError
from app.modules.norms.calculator import (
    CalcRequest,
    LivestockItem,
    NormFact,
    ParamSnapshot,
    TariffFact,
    calculate,
    max_sb,
)

PARAMS = {
    "bhm": "412000",
    "safety_reserve": "0.85",
    "sb_feed_norm": "3.74",
    "season_share": "1.0",
    "rounding_money": {"mode": "half_up", "step": 1},
    "rounding_heads": {"mode": "floor"},
    "coef_sb:sheep_goat_6m": "1.0",
    "coef_sb:cattle_adult": "6.0",
    "tariff_group:sheep_goat_6m": "small_adult",
    "tariff_group:cattle_adult": "large_adult",
}

GRAZING_TARIFFS = (
    TariffFact(
        id=None,
        livestock_group="small_adult",
        coefficient=Decimal("0.1"),
        quantity_unit="head",
        benefit_modifiers=None,
    ),
    TariffFact(
        id=None,
        livestock_group="large_adult",
        coefficient=Decimal("0.45"),
        quantity_unit="head",
        benefit_modifiers=None,
    ),
)


def test_max_sb_follows_vmq_689() -> None:
    """10 ha × 12 c/ha = 120 c; × 0.85 = 102; ÷ 3.74 = 27.27 → floor 27."""
    assert max_sb(area_ha=Decimal("10"), yield_c_per_ha=Decimal("12"), params=PARAMS) == 27


def test_max_sb_floors_and_never_rounds_up() -> None:
    """Ruling 19: a limit is never rounded in the applicant's favour."""
    assert max_sb(area_ha=Decimal("1"), yield_c_per_ha=Decimal("4.4"), params=PARAMS) == 1


def test_grazing_amount_is_per_head_per_group() -> None:
    """50 sheep at 0.1 BHM + 3 cows at 0.45 BHM, BHM = 412 000:
    50 × 0.1 × 412000 = 2 060 000; 3 × 0.45 × 412000 = 556 200; total 2 616 200."""
    result = calculate(
        CalcRequest(
            activity_code="grazing",
            on_date=date(2026, 8, 30),
            period_from=date(2026, 5, 1),
            period_to=date(2026, 9, 30),
            area_ha=Decimal("10"),
            items=(
                LivestockItem("sheep_goat_6m", 50),
                LivestockItem("cattle_adult", 3),
            ),
            quantity=None,
            benefit_code=None,
        ),
        ParamSnapshot(
            values=PARAMS,
            tariffs=GRAZING_TARIFFS,
            norm=NormFact(
                id=None,
                yield_c_per_ha=Decimal("12"),
                max_sb=27,
                season={"windows": [{"from": "04-01", "to": "10-31"}]},
                rotation={"rest_years": []},
            ),
            load_sb=Decimal("0"),
            load_source="none",
        ),
    )
    assert result.amount == Decimal("2616200")
    assert result.used_sb == Decimal("68.0")  # 50 × 1.0 + 3 × 6.0
    assert result.max_sb == 27
    assert result.remaining_sb == Decimal("27")


def test_the_bhm_of_the_day_is_the_one_used() -> None:
    """Ruling 7: a calculation on 2026-09-02 uses 440 000, not 412 000."""
    request = CalcRequest(
        activity_code="haymaking",
        on_date=date(2026, 9, 2),
        period_from=date(2026, 6, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("10"),
        items=(),
        quantity=Decimal("4"),
        benefit_code=None,
    )
    snapshot = ParamSnapshot(
        values=PARAMS | {"bhm": "440000"},
        tariffs=(
            TariffFact(
                id=None,
                livestock_group=None,
                coefficient=Decimal("1.5"),
                quantity_unit="ha",
                benefit_modifiers=None,
            ),
        ),
        norm=None,
        load_sb=Decimal("0"),
        load_source="none",
    )
    # 4 ha × 1.5 × 440 000 = 2 640 000
    assert calculate(request, snapshot).amount == Decimal("2640000")


def test_a_benefit_multiplies_the_coefficient_and_is_recorded() -> None:
    request = CalcRequest(
        activity_code="haymaking",
        on_date=date(2026, 8, 30),
        period_from=date(2026, 6, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("10"),
        items=(),
        quantity=Decimal("4"),
        benefit_code="veteran",
    )
    snapshot = ParamSnapshot(
        values=PARAMS,
        tariffs=(
            TariffFact(
                id=None,
                livestock_group=None,
                coefficient=Decimal("1.5"),
                quantity_unit="ha",
                benefit_modifiers={"veteran": "0.5"},
            ),
        ),
        norm=None,
        load_sb=Decimal("0"),
        load_source="none",
    )
    result = calculate(request, snapshot)
    assert result.amount == Decimal("1236000")  # 4 × 0.75 × 412 000
    benefit = next(line for line in result.breakdown if line["kind"] == "benefit")
    assert benefit["modifier"] == "0.5"
    assert benefit["coefficient_after"] == "0.750000"


def test_an_unknown_benefit_code_is_rejected_not_ignored() -> None:
    """Ruling 20: silently charging full price for a claimed benefit is the worst
    of the three possible behaviours."""
    request = CalcRequest(
        activity_code="haymaking",
        on_date=date(2026, 8, 30),
        period_from=date(2026, 6, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("10"),
        items=(),
        quantity=Decimal("4"),
        benefit_code="nonexistent",
    )
    snapshot = ParamSnapshot(
        values=PARAMS,
        tariffs=(
            TariffFact(
                id=None,
                livestock_group=None,
                coefficient=Decimal("1.5"),
                quantity_unit="ha",
                benefit_modifiers={"veteran": "0.5"},
            ),
        ),
        norm=None,
        load_sb=Decimal("0"),
        load_source="none",
    )
    with pytest.raises(DomainError) as raised:
        calculate(request, snapshot)
    assert raised.value.code == "ERR-VAL-001"


def test_a_missing_parameter_names_itself() -> None:
    """Ruling 6/8: with `coef_sb:*` still in draft, a grazing calculation must say
    exactly which number is missing — not fall back to a literal."""
    request = CalcRequest(
        activity_code="grazing",
        on_date=date(2026, 8, 30),
        period_from=date(2026, 5, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("10"),
        items=(LivestockItem("sheep_goat_6m", 10),),
        quantity=None,
        benefit_code=None,
    )
    values = {k: v for k, v in PARAMS.items() if k != "coef_sb:sheep_goat_6m"}
    snapshot = ParamSnapshot(
        values=values,
        tariffs=GRAZING_TARIFFS,
        norm=NormFact(id=None, yield_c_per_ha=Decimal("12"), max_sb=27, season=None, rotation=None),
        load_sb=Decimal("0"),
        load_source="none",
    )
    with pytest.raises(DomainError) as raised:
        calculate(request, snapshot)
    assert raised.value.code == "ERR-NORM-004"
    assert raised.value.details == {"code": "coef_sb:sheep_goat_6m"}


def test_science_has_no_tariff_and_costs_nothing() -> None:
    """Ruling 1: VMQ 278 has no rate for research; it is contracted separately."""
    request = CalcRequest(
        activity_code="science",
        on_date=date(2026, 8, 30),
        period_from=date(2026, 6, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("10"),
        items=(),
        quantity=Decimal("1"),
        benefit_code=None,
    )
    result = calculate(
        request,
        ParamSnapshot(
            values=PARAMS, tariffs=(), norm=None, load_sb=Decimal("0"), load_source="none"
        ),
    )
    assert result.amount == Decimal("0")
    assert result.breakdown[0]["reason"] == "no_tariff_by_law"


def test_money_rounds_half_up_to_a_whole_sum() -> None:
    """`tz/06`: money rounds to a whole sum, ≥0.5 upwards. 0.5 must not become 0
    the way Python's default banker's rounding would."""
    request = CalcRequest(
        activity_code="haymaking",
        on_date=date(2026, 8, 30),
        period_from=date(2026, 6, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("1"),
        items=(),
        quantity=Decimal("1"),
        benefit_code=None,
    )
    snapshot = ParamSnapshot(
        values=PARAMS | {"bhm": "1"},
        tariffs=(
            TariffFact(
                id=None,
                livestock_group=None,
                coefficient=Decimal("0.5"),
                quantity_unit="ha",
                benefit_modifiers=None,
            ),
        ),
        norm=None,
        load_sb=Decimal("0"),
        load_source="none",
    )
    assert calculate(request, snapshot).amount == Decimal("1")


def test_the_result_carries_the_rule_version_and_a_serializable_snapshot() -> None:
    import json

    request = CalcRequest(
        activity_code="haymaking",
        on_date=date(2026, 8, 30),
        period_from=date(2026, 6, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("10"),
        items=(),
        quantity=Decimal("4"),
        benefit_code=None,
    )
    snapshot = ParamSnapshot(
        values=PARAMS,
        tariffs=(
            TariffFact(
                id=None,
                livestock_group=None,
                coefficient=Decimal("1.5"),
                quantity_unit="ha",
                benefit_modifiers=None,
            ),
        ),
        norm=None,
        load_sb=Decimal("0"),
        load_source="none",
    )
    result = calculate(request, snapshot)
    assert result.rule_code_version == "norms-1.0.0"
    json.dumps(result.input_snapshot)  # must not raise: Decimal/date are already strings
