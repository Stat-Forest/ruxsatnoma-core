"""norms: the tariff-exemption parameter and the missing quantity_unit CHECK.

Two fixes from stage 3.7's final whole-branch review, neither of which may edit
0011/0012 in place (both are already applied):

* **C2** — a missing FLAT tariff used to bill zero under `no_tariff_by_law`,
  which is true for `science` and false for haymaking/apiary/deadwood/
  recreation, all of which carry a published VMQ 278 rate. The exemption is now
  an explicit, versioned fact — `tariff_exempt:<activity_code>` — read by
  `norms.calculator._is_tariff_exempt`; every other missing flat row raises
  `ERR-NORM-004` naming itself. Seeded for `science` only, from the same
  2015-09-30 the VMQ 278 rates start at, so a calculation dated to any day
  those rates cover finds the exemption in force too.
* **I8 / deferred minor #2** — `tariffs.quantity_unit` had no CHECK at all,
  unlike its sibling `activity_types.quantity_unit`, so any string could be
  stored and copied verbatim into an immutable calculation's `breakdown`. The
  allowed set is `admin.models.QUANTITY_UNITS`, the same list 0011 widened the
  activity_types CHECK to.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-01 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUANTITY_UNITS = ("head", "ton", "hive", "ha", "person_day", "m3", "unit")

TARIFF_EXEMPT = [
    (
        "tariff_exempt:science",
        "true",
        "ВМҚ 278-сон, 30.09.2015 — илмий-тадқиқот учун ставка йўқ (алоҳида шартнома)",
    ),
]


def upgrade() -> None:
    units = ", ".join(f"'{unit}'" for unit in QUANTITY_UNITS)
    op.execute(
        "ALTER TABLE tariffs ADD CONSTRAINT ck_tariffs_quantity_unit_valid "
        f"CHECK (quantity_unit IN ({units}))"
    )
    for code, value, basis in TARIFF_EXEMPT:
        op.execute(
            sa.text(
                "INSERT INTO rule_parameters (id, code, value, effective_from, basis, status) "
                "VALUES (gen_random_uuid(), :code, to_jsonb(CAST(:value AS text)), "
                "DATE '2015-09-30', :basis, 'published')"
            ).bindparams(code=code, value=value, basis=basis)
        )


def downgrade() -> None:
    # Scoped to `created_by IS NULL` for the same reason 0012's own downgrade is
    # (finding I3): a delete keyed on content alone would also destroy a row an
    # admin later published for a NEW un-tariffed activity. Only a migration
    # inserts with no maker.
    op.execute(
        sa.text(
            "DELETE FROM rule_parameters WHERE code = ANY(:codes) AND created_by IS NULL"
        ).bindparams(codes=[code for code, _, _ in TARIFF_EXEMPT])
    )
    op.execute("ALTER TABLE tariffs DROP CONSTRAINT ck_tariffs_quantity_unit_valid")
