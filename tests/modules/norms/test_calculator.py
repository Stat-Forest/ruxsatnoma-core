"""The arithmetic, in isolation. Every number here is traceable to a primary
source: VMQ 689 for the limit (0.85, 3.74), VMQ 278 for the rates, and the real
BHM (412 000 sum until 2026-08-31, 440 000 from 2026-09-01)."""

import json
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.errors import DomainError
from app.modules.norms.calculator import (
    GRAZING,
    CalcRequest,
    LivestockItem,
    NormFact,
    ParamSnapshot,
    TariffFact,
    calculate,
    from_input_snapshot,
    max_sb,
    remaining_sb,
    resolve_capacity,
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
    # C2: the ONLY thing that makes a missing flat tariff a lawful zero rather
    # than a gap in our own table — seeded published by migration 0013.
    "tariff_exempt:science": "true",
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
    """Ruling 1: VMQ 278 has no rate for research; it is contracted separately.

    C2: the zero is now reached through the EXPLICIT `tariff_exempt:science`
    parameter (seeded by 0013), never through the mere absence of a row —
    `test_a_flat_activity_with_no_exemption_raises_instead_of_billing_zero`
    below is the other half of that pair."""
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


def test_a_flat_activity_with_no_exemption_raises_instead_of_billing_zero() -> None:
    """C2, the mirror of the grazing test above on the OTHER branch of
    `calculate`. `haymaking` (like apiary, deadwood and recreation) HAS a
    published VMQ 278 rate, and changing a rate requires archive-then-publish,
    so a window with no effective row is reachable through the documented
    workflow. Billing zero there — with `reason="no_tariff_by_law"` on an
    append-only row an invoice is later built from — is the silent
    under-billing this stage's own global constraint forbids."""
    request = CalcRequest(
        activity_code="haymaking",
        on_date=date(2026, 8, 30),
        period_from=date(2026, 6, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("10"),
        items=(),
        quantity=Decimal("5"),
        benefit_code=None,
    )
    snapshot = ParamSnapshot(
        values=PARAMS, tariffs=(), norm=None, load_sb=Decimal("0"), load_source="none"
    )
    with pytest.raises(DomainError) as raised:
        calculate(request, snapshot)
    assert raised.value.code == "ERR-NORM-004"
    assert raised.value.details == {"code": "tariff:haymaking"}


def test_the_exemption_must_say_true_not_merely_exist() -> None:
    """Fail-closed (ruling 6's own shape): the exemption is a VALUE, so a row
    published with anything but `true` — a `"false"` left over from an
    activity that has since acquired a rate, a typo — leaves the activity
    billable and the missing row loud."""
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
    snapshot = ParamSnapshot(
        values=PARAMS | {"tariff_exempt:science": "false"},
        tariffs=(),
        norm=None,
        load_sb=Decimal("0"),
        load_source="none",
    )
    with pytest.raises(DomainError) as raised:
        calculate(request, snapshot)
    assert raised.value.code == "ERR-NORM-004"
    assert raised.value.details == {"code": "tariff:science"}


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


def test_remaining_sb_is_floored_like_every_other_limit() -> None:
    """I1 (final review): ruling 19 names `remaining_sb` alongside `max_sb` —
    "never round a limit in the applicant's favour" — but only `max_sb` was
    ever rounded. Invisible today (`LOAD_PROVIDERS` is empty, so `load_sb` is
    always 0 and `remaining_sb == max_sb` exactly); the moment 3.11 registers
    a real provider the load turns fractional (the `coef_sb` scale runs down
    to 0.2 for a lamb) and an unfloored remainder grants the applicant the
    fraction of a conditional head the ruling says to take away."""
    assert remaining_sb(27, Decimal("3.5"), PARAMS) == Decimal("23")
    assert remaining_sb(27, Decimal("0"), PARAMS) == Decimal("27")


def test_remaining_sb_reads_the_rounding_mode_like_max_sb_does() -> None:
    """The MODE is a parameter here too, not a hardcoded floor — the same
    thing finding 3 of fix round 1 established for `max_sb`."""
    half_up = PARAMS | {"rounding_heads": {"mode": "half_up"}}
    assert remaining_sb(27, Decimal("3.4"), half_up) == Decimal("24")
    assert remaining_sb(27, Decimal("3.4"), PARAMS) == Decimal("23")


def test_the_calculated_remaining_sb_is_the_rounded_one() -> None:
    """One place, one number: `calculate` must store what `remaining_sb`
    returns, not its own unrounded subtraction (the two used to recompute it
    identically, which is how they would have drifted)."""
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
    assert result.remaining_sb == Decimal("23")
    limit_line = next(line for line in result.breakdown if line["kind"] == "limit")
    assert limit_line["remaining_sb"] == "23"


# --- Ruling #176 (stage 9): `resolve_capacity`, the one place a caller picks
# between grazing's `max_sb` and every other activity's own `capacity`.


def test_resolve_capacity_reads_max_sb_for_grazing() -> None:
    norm = NormFact(id=None, yield_c_per_ha=Decimal("12"), max_sb=250, season=None, rotation=None)
    assert resolve_capacity(GRAZING, norm) == Decimal("250")


def test_resolve_capacity_ignores_a_capacity_value_for_grazing() -> None:
    """Grazing reads `max_sb` alone — a `capacity` value sitting on the same
    row (which `service.create_norm`/`update_norm` refuse to write, but a
    row inserted before that guard existed could still carry) is not read,
    so there is never a second source of truth in force at once."""
    norm = NormFact(
        id=None,
        yield_c_per_ha=Decimal("12"),
        max_sb=250,
        season=None,
        rotation=None,
        capacity=Decimal("999"),
    )
    assert resolve_capacity(GRAZING, norm) == Decimal("250")


def test_resolve_capacity_is_none_when_max_sb_is_unset() -> None:
    """A published grazing norm with no frozen `max_sb` — ruling #176's
    EXCLUSIVE trigger, not "unlimited"."""
    norm = NormFact(id=None, yield_c_per_ha=None, max_sb=None, season=None, rotation=None)
    assert resolve_capacity(GRAZING, norm) is None


def test_resolve_capacity_reads_capacity_for_every_other_activity() -> None:
    norm = NormFact(
        id=None,
        yield_c_per_ha=None,
        max_sb=None,
        season=None,
        rotation=None,
        capacity=Decimal("10.5"),
    )
    assert resolve_capacity("haymaking", norm) == Decimal("10.5")


def test_resolve_capacity_is_none_when_capacity_is_unset() -> None:
    norm = NormFact(id=None, yield_c_per_ha=None, max_sb=None, season=None, rotation=None)
    assert resolve_capacity("haymaking", norm) is None


def test_resolve_capacity_is_none_with_no_norm_at_all() -> None:
    """No norm and no capacity are the SAME fact to this function — both mean
    "nothing to compare against", for grazing and every other activity."""
    assert resolve_capacity(GRAZING, None) is None
    assert resolve_capacity("haymaking", None) is None


def test_a_stored_calculation_recomputes_to_the_same_numbers() -> None:
    """I9 (final review), and the stage's headline guarantee (ruling 18):
    "every stored calculation carries `rule_version` + `input_snapshot`
    sufficient to recompute it years later". Until now the tests asserted
    only that particular KEYS were present — which is precisely how the
    missing `load_sb`/`load_source` went unnoticed until fix-round 1 added
    them. This rebuilds the request and the snapshot from the stored column
    ALONE, recomputes, and compares every number.

    Deliberately the richest shape available: grazing (two groups, so both
    tariff rows and both `coef_sb` coefficients matter), a claimed benefit
    that applies to only one of them, a norm with a frozen `max_sb`, and a
    non-zero committed load — the last being the one `remaining_sb` cannot be
    reproduced without."""
    request = CalcRequest(
        activity_code="grazing",
        on_date=date(2026, 8, 30),
        period_from=date(2026, 5, 1),
        period_to=date(2026, 9, 30),
        area_ha=Decimal("92.0000"),
        items=(LivestockItem("sheep_goat_6m", 50), LivestockItem("cattle_adult", 3)),
        quantity=None,
        benefit_code="veteran",
    )
    tariffs = (
        TariffFact(
            id=uuid.uuid4(),
            livestock_group="small_adult",
            coefficient=Decimal("0.1"),
            quantity_unit="head",
            benefit_modifiers={"veteran": "0.5"},
        ),
        TariffFact(
            id=uuid.uuid4(),
            livestock_group="large_adult",
            coefficient=Decimal("0.45"),
            quantity_unit="head",
            benefit_modifiers=None,
        ),
    )
    snapshot = ParamSnapshot(
        values=PARAMS,
        tariffs=tariffs,
        norm=NormFact(
            id=uuid.uuid4(),
            yield_c_per_ha=Decimal("12"),
            max_sb=250,
            season={"windows": [{"from": "04-01", "to": "10-31"}]},
            rotation={"rest_years": [2027]},
        ),
        load_sb=Decimal("3.5"),
        load_source="permits",
    )
    original = calculate(request, snapshot)

    # Round-trip through JSON, because that is what the JSONB column does to
    # it — nothing may depend on a Python object that survived in memory.
    stored = json.loads(json.dumps(original.input_snapshot))
    assert stored["rule_code_version"] == original.rule_code_version

    rebuilt_request, rebuilt_snapshot = from_input_snapshot(stored)
    recomputed = calculate(rebuilt_request, rebuilt_snapshot)

    assert recomputed.amount == original.amount
    assert recomputed.used_sb == original.used_sb
    assert recomputed.max_sb == original.max_sb
    assert recomputed.remaining_sb == original.remaining_sb
    assert recomputed.breakdown == original.breakdown
    # And it is a real number, not two matching Nones.
    assert original.amount > 0
    assert original.remaining_sb is not None


def test_the_recomputation_survives_the_shape_the_database_returns() -> None:
    """The same guarantee against a snapshot read back OUT of Postgres rather
    than the dict `calculate` just built: JSONB preserves the strings
    `jsonable` wrote, and the rebuild must not depend on anything else."""
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
    original = calculate(request, snapshot)
    assert original.amount == Decimal("2472000")  # 4 ha × 1.5 × 412 000

    rebuilt = calculate(*from_input_snapshot(json.loads(json.dumps(original.input_snapshot))))
    assert rebuilt.amount == original.amount
    assert rebuilt.breakdown == original.breakdown
