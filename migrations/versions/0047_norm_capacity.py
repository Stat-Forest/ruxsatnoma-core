"""norm capacity

Stage 9, ruling #176 (docs/decisions.md): a norm's capacity generalises beyond
grazing's `max_sb`. Adds a nullable `capacity` column to `norms`, in the
activity's own `quantity_unit` (ha/hive/m3/person_day) — grazing keeps using
`max_sb` alone, never this column (enforced in `norms.service`, not the
database: a CHECK cannot join `activity_types` to know the activity's code).

Revision ID: 0047
Revises: 0046
Create Date: 2026-09-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0047"
down_revision: str | Sequence[str] | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("norms", sa.Column("capacity", sa.Numeric(precision=14, scale=4), nullable=True))
    op.create_check_constraint("capacity_valid", "norms", "capacity IS NULL OR capacity >= 0")


def downgrade() -> None:
    """Downgrade schema."""
    # Short name here, not the fully-qualified "ck_norms_capacity_valid" —
    # drop_constraint re-runs it through the naming convention just like
    # create_check_constraint does (0038/0007's own note).
    op.drop_constraint("capacity_valid", "norms", type_="check")
    op.drop_column("norms", "capacity")
