"""updated_at list indexes

`GET /applications`, `GET /permits` and `GET /inspections/cases` now list
most recently UPDATED first (`updated_at DESC, id DESC`) instead of newest-
created first: an operator's queue reads as "what changed since I looked", and
an old application that was just returned, a permit that was just suspended
or a case whose violator was just asked to explain belongs at the top, not
buried under everything created after it.

The old order was served by the primary key (uuid7 is creation-ordered); the
new one needs its own btree per table, `(updated_at, id)`, which Postgres
walks backward for the DESC page and stops early on the LIMIT — otherwise a
republic-scoped reader's first page is a full sort of the table. Both columns
are NOT NULL on all three tables, so each index is a pure addition and its
downgrade a plain drop.

Revision ID: 0057
Revises: 0056
Create Date: 2026-09-10 18:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0057"
down_revision: str | Sequence[str] | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEXES = (
    ("ix_applications_updated_at_id", "applications"),
    ("ix_permits_updated_at_id", "permits"),
    ("ix_violation_cases_updated_at_id", "violation_cases"),
)


def upgrade() -> None:
    """Upgrade schema."""
    for name, table in _INDEXES:
        op.create_index(name, table, ["updated_at", "id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    for name, table in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
