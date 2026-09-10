"""permit quantity

Stage 9, ruling #176 (docs/decisions.md), the enforcement half (T6). Adds a
nullable `quantity` column to `permits` — the non-grazing sibling of the
existing `sb_load`, in the activity's own `quantity_unit`, mirroring
`norms.capacity`'s own scale (migration 0047). Captured once at issuance and
never recomputed; NULL for grazing (whose committed load is `sb_load` alone)
and for any activity priced at a declared quantity of zero.

Revision ID: 0049
Revises: 0048
Create Date: 2026-09-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0049"
down_revision: str | Sequence[str] | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "permits", sa.Column("quantity", sa.Numeric(precision=14, scale=4), nullable=True)
    )
    op.create_check_constraint("quantity_valid", "permits", "quantity IS NULL OR quantity >= 0")


def downgrade() -> None:
    """Downgrade schema."""
    # Short name here, not the fully-qualified "ck_permits_quantity_valid" —
    # drop_constraint re-runs it through the naming convention just like
    # create_check_constraint does (0038/0007's own note, repeated by 0047).
    op.drop_constraint("quantity_valid", "permits", type_="check")
    op.drop_column("permits", "quantity")
