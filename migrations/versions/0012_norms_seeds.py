"""VMQ 278 tariffs and VMQ 689 parameters.

Read from the primary sources on 2026-08-30:
  https://lex.uz/docs/2770948  (VMQ 278 of 30.09.2015, the rate annex)
  https://lex.uz/docs/4477902  (VMQ 689 of 19.08.2019, the grazing-norm formula)
BHM values: 412 000 sum from 2025-08-01, 440 000 from 2026-09-01 (PF-115 of 23.06.2026).

The ten `coef_sb:*` rows are DRAFT on purpose (plan 03.7 ruling 8): VMQ 689's annex
5 is truncated on lex.uz and has not arrived from the Agency, so the values below
are conventional placeholders. The calculator reads published rows only, so a
grazing calculation raises ERR-NORM-004 until a central admin publishes them.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-30 14:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VMQ_278 = "ВМҚ 278-сон, 30.09.2015, илова"
VMQ_689 = "ВМҚ 689-сон, 19.08.2019"

TARIFFS = [
    # (activity code, livestock group, coefficient, quantity unit)
    ("grazing", "large_adult", "0.45", "head"),
    ("grazing", "large_young", "0.15", "head"),
    ("grazing", "small_adult", "0.10", "head"),
    ("grazing", "small_young", "0.03", "head"),
    ("haymaking", None, "1.50", "ha"),
    ("apiary", None, "0.05", "hive"),
    ("deadwood", None, "0.40", "m3"),
    ("recreation", None, "0.05", "person_day"),
]

# Which VMQ 278 rate row a livestock type is charged at (ruling 9).
TARIFF_GROUPS = {
    "cattle_adult": "large_adult",
    "horse_adult": "large_adult",
    "camel_adult": "large_adult",
    "donkey_adult": "large_adult",
    "cattle_young": "large_young",
    "horse_young": "large_young",
    "camel_young": "large_young",
    "donkey_young": "large_young",
    "sheep_goat_6m": "small_adult",
    "lamb_kid_under_6m": "small_young",
}

# Provisional — VMQ 689 annex 5 has not arrived (ruling 8). One conditional head
# is one karakul or Romanov sheep, which is why the small adult group is 1.0.
COEF_SB = {
    "cattle_adult": "6.0",
    "cattle_young": "3.0",
    "horse_adult": "7.0",
    "horse_young": "3.5",
    "camel_adult": "8.0",
    "camel_young": "4.0",
    "donkey_adult": "4.0",
    "donkey_young": "2.0",
    "sheep_goat_6m": "1.0",
    "lamb_kid_under_6m": "0.2",
}

PARAMETERS = [
    # (code, value, unit, effective_from, effective_to, basis)
    ("bhm", "412000", "sum", "2025-08-01", "2026-08-31", "БҲМ, амалдаги миқдор"),
    ("bhm", "440000", "sum", "2026-09-01", None, "ПФ-115, 23.06.2026"),
    ("safety_reserve", "0.85", None, "2019-08-19", None, VMQ_689),
    ("sb_feed_norm", "3.74", "centner", "2019-08-19", None, VMQ_689),
    (
        "season_share",
        "1.0",
        None,
        "2019-08-19",
        None,
        f"{VMQ_689} — no seasonal factor in the decree",
    ),
    ("rounding_money", '{"mode": "half_up", "step": 1}', "sum", "2015-09-30", None, "tz/06"),
    ("rounding_heads", '{"mode": "floor"}', None, "2015-09-30", None, "tz/06"),
]


def upgrade() -> None:
    op.execute("UPDATE activity_types SET quantity_unit = 'ha' WHERE code = 'haymaking'")
    op.execute("UPDATE activity_types SET quantity_unit = 'person_day' WHERE code = 'recreation'")
    op.execute("UPDATE activity_types SET quantity_unit = 'm3' WHERE code = 'deadwood'")

    for code, value, unit, effective_from, effective_to, basis in PARAMETERS:
        # NOTE: "to_jsonb(:value::text)" would look equivalent but SQLAlchemy's
        # text() bind-param regex is `(?<![:\w\x5c]):(\w+)(?!:)` — a name
        # directly followed by "::" fails the trailing negative lookahead, so
        # the greedy \w+ backtracks one character and registers "valu" (the
        # name minus its last letter) instead of "value". CAST(... AS text)
        # has no adjacent "::" and sidesteps the quirk entirely.
        as_json = (
            "CAST(:value AS jsonb)" if value.startswith("{") else "to_jsonb(CAST(:value AS text))"
        )
        op.execute(
            sa.text(
                f"INSERT INTO rule_parameters (id, code, value, unit, effective_from, "
                f"effective_to, basis, status) VALUES (gen_random_uuid(), :code, {as_json}, "
                f":unit, CAST(:ef AS date), CAST(:et AS date), :basis, 'published')"
            ).bindparams(
                code=code, value=value, unit=unit, ef=effective_from, et=effective_to, basis=basis
            )
        )

    for livestock_code, group in TARIFF_GROUPS.items():
        op.execute(
            sa.text(
                "INSERT INTO rule_parameters (id, code, value, effective_from, basis, status) "
                "VALUES (gen_random_uuid(), :code, to_jsonb(CAST(:value AS text)), "
                "DATE '2015-09-30', :basis, 'published')"
            ).bindparams(code=f"tariff_group:{livestock_code}", value=group, basis=VMQ_278)
        )

    for livestock_code, coefficient in COEF_SB.items():
        op.execute(
            sa.text(
                "INSERT INTO rule_parameters (id, code, value, effective_from, basis, status) "
                "VALUES (gen_random_uuid(), :code, to_jsonb(CAST(:value AS text)), "
                "DATE '2019-08-19', :basis, 'draft')"
            ).bindparams(
                code=f"coef_sb:{livestock_code}",
                value=coefficient,
                basis="provisional — awaiting VMQ 689 annex 5",
            )
        )

    for activity_code, group, coefficient, unit in TARIFFS:
        op.execute(
            sa.text(
                "INSERT INTO tariffs (id, activity_type_id, livestock_group, coefficient, "
                "quantity_unit, effective_from, basis, status) "
                "SELECT gen_random_uuid(), id, :group, CAST(:coefficient AS numeric), :unit, "
                "DATE '2015-09-30', :basis, 'published' FROM activity_types WHERE code = :activity"
            ).bindparams(
                activity=activity_code,
                group=group,
                coefficient=coefficient,
                unit=unit,
                basis=VMQ_278,
            )
        )


def downgrade() -> None:
    op.execute(f"DELETE FROM tariffs WHERE basis = '{VMQ_278}'")
    op.execute(
        "DELETE FROM rule_parameters WHERE code LIKE 'coef_sb:%' OR code LIKE 'tariff_group:%' "
        "OR code IN ('bhm', 'safety_reserve', 'sb_feed_norm', 'season_share', "
        "'rounding_money', 'rounding_heads')"
    )
    op.execute("UPDATE activity_types SET quantity_unit = 'ton' WHERE code = 'haymaking'")
    op.execute("UPDATE activity_types SET quantity_unit = 'ha' WHERE code = 'recreation'")
    op.execute("UPDATE activity_types SET quantity_unit = 'ton' WHERE code = 'deadwood'")
