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


# --- Fix-round 1: findings 1, 2, 3, 4, 6 --------------------------------


def test_an_unknown_benefit_is_rejected_when_grazing_has_no_items() -> None:
    """Finding 1, leak path 1: a claimed benefit used to leak through with no
    error whenever `calculate` never resolved a single tariff row to check it
    against — an empty `items` on a grazing request is exactly that case, and
    `input_snapshot` would otherwise have recorded a benefit that was never
    actually validated as if it had been honoured."""
    request = CalcRequest(
        activity_code="grazing",
        on_date=date(2026, 8, 30),
        period_from=date(2026, 5, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("10"),
        items=(),
        quantity=None,
        benefit_code="totally_bogus",
    )
    snapshot = ParamSnapshot(
        values=PARAMS, tariffs=GRAZING_TARIFFS, norm=None, load_sb=Decimal("0"), load_source="none"
    )
    with pytest.raises(DomainError) as raised:
        calculate(request, snapshot)
    assert raised.value.code == "ERR-VAL-001"
    assert raised.value.details == {"reason": "unknown_benefit_code", "code": "totally_bogus"}


def test_an_unknown_benefit_is_rejected_when_the_activity_has_no_tariff_at_all() -> None:
    """Finding 1, leak path 2: `science` (or any flat activity with zero
    published tariff rows) used to accept a bogus benefit code silently,
    because `_apply_benefit` never even ran to check it."""
    request = CalcRequest(
        activity_code="science",
        on_date=date(2026, 8, 30),
        period_from=date(2026, 6, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("10"),
        items=(),
        quantity=Decimal("1"),
        benefit_code="totally_bogus",
    )
    snapshot = ParamSnapshot(
        values=PARAMS, tariffs=(), norm=None, load_sb=Decimal("0"), load_source="none"
    )
    with pytest.raises(DomainError) as raised:
        calculate(request, snapshot)
    assert raised.value.code == "ERR-VAL-001"
    assert raised.value.details == {"reason": "unknown_benefit_code", "code": "totally_bogus"}


def test_a_benefit_valid_for_only_some_grazing_groups_still_applies_where_it_exists() -> None:
    """The union design behind finding 1's fix: a code recognised by AT LEAST
    ONE resolved row is accepted for the whole request; `_apply_benefit` then
    decides per row whether it actually grants a discount there — not
    all-or-nothing. sheep get the veteran discount, cattle do not carry it at
    all and are billed in full, and neither is an error."""
    tariffs = (
        TariffFact(
            id=None,
            livestock_group="small_adult",
            coefficient=Decimal("0.1"),
            quantity_unit="head",
            benefit_modifiers={"veteran": "0.5"},
        ),
        TariffFact(
            id=None,
            livestock_group="large_adult",
            coefficient=Decimal("0.45"),
            quantity_unit="head",
            benefit_modifiers=None,
        ),
    )
    request = CalcRequest(
        activity_code="grazing",
        on_date=date(2026, 8, 30),
        period_from=date(2026, 5, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("10"),
        items=(LivestockItem("sheep_goat_6m", 10), LivestockItem("cattle_adult", 2)),
        quantity=None,
        benefit_code="veteran",
    )
    snapshot = ParamSnapshot(
        values=PARAMS, tariffs=tariffs, norm=None, load_sb=Decimal("0"), load_source="none"
    )
    result = calculate(request, snapshot)
    # sheep: 10 × 0.1 × 0.5 × 412000 = 206000; cattle: 2 × 0.45 × 412000 = 370800 (unchanged)
    assert result.amount == Decimal("576800")
    benefit_lines = [line for line in result.breakdown if line["kind"] == "benefit"]
    assert len(benefit_lines) == 1
    assert benefit_lines[0]["livestock_code"] == "sheep_goat_6m"


def test_a_missing_grazing_tariff_row_raises_instead_of_billing_zero() -> None:
    """Finding 2: VMQ 278 has a published rate for all four grazing groups,
    so a missing row here is a GAP in our own tariff table (an archived row
    whose replacement has not started yet — legal under the EXCLUDE index),
    never the law being silent. Billing zero under "no_tariff_by_law" would
    be silent under-billing with the audit trail asserting lawfulness."""
    request = CalcRequest(
        activity_code="grazing",
        on_date=date(2026, 8, 30),
        period_from=date(2026, 5, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("10"),
        items=(LivestockItem("cattle_adult", 3),),
        quantity=None,
        benefit_code=None,
    )
    # Only small_adult is published; large_adult (cattle_adult's own group)
    # has no row at all.
    tariffs = (
        TariffFact(
            id=None,
            livestock_group="small_adult",
            coefficient=Decimal("0.1"),
            quantity_unit="head",
            benefit_modifiers=None,
        ),
    )
    snapshot = ParamSnapshot(
        values=PARAMS, tariffs=tariffs, norm=None, load_sb=Decimal("0"), load_source="none"
    )
    with pytest.raises(DomainError) as raised:
        calculate(request, snapshot)
    assert raised.value.code == "ERR-NORM-004"
    assert raised.value.details == {"code": "tariff:grazing:large_adult"}


def test_max_sb_reads_rounding_heads_half_up_not_a_hardcoded_floor() -> None:
    """Finding 3: `rounding_heads` used to be loaded into every snapshot and
    never read — `max_sb` hardcoded floor regardless of what it said. 3 ha ×
    1 c/ha × 1.0 season_share × 1.0 safety_reserve / 2 sb_feed_norm = 1.5:
    floor gives 1, half_up gives 2 — proving the parameter is actually
    consulted now, not merely present."""
    base = {"season_share": "1", "safety_reserve": "1", "sb_feed_norm": "2"}
    floor_params = base | {"rounding_heads": {"mode": "floor"}}
    half_up_params = base | {"rounding_heads": {"mode": "half_up"}}
    assert max_sb(area_ha=Decimal("3"), yield_c_per_ha=Decimal("1"), params=floor_params) == 1
    assert max_sb(area_ha=Decimal("3"), yield_c_per_ha=Decimal("1"), params=half_up_params) == 2


def test_max_sb_raises_when_rounding_heads_is_missing() -> None:
    """Finding 3: its absence must be as loud as any other missing parameter."""
    params = {k: v for k, v in PARAMS.items() if k != "rounding_heads"}
    with pytest.raises(DomainError) as raised:
        max_sb(area_ha=Decimal("10"), yield_c_per_ha=Decimal("12"), params=params)
    assert raised.value.code == "ERR-NORM-004"
    assert raised.value.details == {"code": "rounding_heads"}


def test_max_sb_raises_when_rounding_heads_mode_is_unrecognised() -> None:
    params = PARAMS | {"rounding_heads": {"mode": "ceil"}}
    with pytest.raises(DomainError) as raised:
        max_sb(area_ha=Decimal("10"), yield_c_per_ha=Decimal("12"), params=params)
    assert raised.value.code == "ERR-NORM-004"
    assert raised.value.details == {"code": "rounding_heads"}


def test_an_unrecognised_rounding_money_mode_raises_rather_than_flooring() -> None:
    """Finding 4: an admin's `{"mode": "HALF_UP"}` typo (wrong case) used to
    fall through to a silent floor — flooring the 0.5-rounds-up boundary DOWN
    to 0 with no error, defeating the one test written to prevent exactly
    that (`test_money_rounds_half_up_to_a_whole_sum`)."""
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
        values=PARAMS | {"bhm": "1", "rounding_money": {"mode": "HALF_UP", "step": 1}},
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
    with pytest.raises(DomainError) as raised:
        calculate(request, snapshot)
    assert raised.value.code == "ERR-NORM-004"
    assert raised.value.details == {"code": "rounding_money"}


def test_rounding_money_with_no_step_raises() -> None:
    """Finding 4's other half: a `rounding_money` republished with a `mode`
    but no `step` used to default to `step=1` silently."""
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
        values=PARAMS | {"bhm": "1", "rounding_money": {"mode": "half_up"}},
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
    with pytest.raises(DomainError) as raised:
        calculate(request, snapshot)
    assert raised.value.code == "ERR-NORM-004"
    assert raised.value.details == {"code": "rounding_money"}


def test_input_snapshot_carries_the_load_so_remaining_sb_is_reproducible() -> None:
    """Finding 6 (controller ruling): remaining_sb = max_sb - load_sb, so
    `input_snapshot` must carry `load_sb`/`load_source` too, or the one
    number this column exists to make reproducible (plan ruling 18) cannot be
    recomputed from the snapshot alone years later."""
    request = CalcRequest(
        activity_code="grazing",
        on_date=date(2026, 8, 30),
        period_from=date(2026, 5, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("10"),
        items=(LivestockItem("sheep_goat_6m", 5),),
        quantity=None,
        benefit_code=None,
    )
    snapshot = ParamSnapshot(
        values=PARAMS,
        tariffs=GRAZING_TARIFFS,
        norm=NormFact(id=None, yield_c_per_ha=Decimal("12"), max_sb=27, season=None, rotation=None),
        load_sb=Decimal("3.5"),
        load_source="permits",
    )
    result = calculate(request, snapshot)
    assert result.input_snapshot["load_sb"] == "3.5"
    assert result.input_snapshot["load_source"] == "permits"
