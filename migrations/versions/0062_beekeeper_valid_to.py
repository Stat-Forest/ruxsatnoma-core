"""beekeeper certificate term

Ruling #217 (docs/decisions.md): the Union's certificate CARRIES a term —
the first real one Odilxon forwarded on 2026-09-13 reads «Действует до
31.12.2025». Ruling #182 had recorded the opposite ("the Union's fields
carry no term") from the field list alone, so `tz/12` #54 is closed by the
document, not by an answer.

One nullable column, `beekeepers.valid_to`. NULL for every row entered
before this migration and for a registrar who leaves it blank: an unknown
term is not an expired one, and the register never invents a date. An
expired row keeps `status='active'` — expiry is a fact about the certificate,
removal is an act of the Union — and reads as `expired` from
`beekeepers.service.match_certificate`.

Revision ID: 0062
Revises: 0060 — stage 15's `0061_permit_blanks` also revises 0060 in a
parallel worktree; whichever lands second re-points `down_revision` to the
other (the chain is never monotonic — CLAUDE.md), and `alembic heads` must
print ONE head before either PR merges.
Create Date: 2026-09-14 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0062"
down_revision: str | Sequence[str] | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("beekeepers", sa.Column("valid_to", sa.Date(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("beekeepers", "valid_to")
