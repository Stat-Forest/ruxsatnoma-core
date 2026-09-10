"""rules acceptance

Stage 10, ruling #184 (docs/decisions.md; wave-2 track B2): "I have read the
rules" is a mandatory acceptance before any signature, recorded on the
application — `applications.service.submit` refuses without
`rules_accepted: true` in the body (`ERR-APP-001`, naming `rules_accepted`
like any other missing field) and stamps the moment from the SERVER clock,
never the client's.

One nullable column, `applications.rules_accepted_at`. NULL for every
DRAFT/RETURNED row (nothing has been accepted yet) and for every row
submitted before this migration ran — a historical submission is not
retroactively un-accepted, and nothing reads this column as a gate on
anything but a NEW submission.

The text the checkbox links to is a `system_settings` row
(`site_rules_url`), not a schema change — `app/core/settings_store.py`'s
own `SETTING_SPECS` dict, no migration needed: a missing row already means
"use the code default" for every setting in this codebase.

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-10 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0055"
down_revision: str | Sequence[str] | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "applications",
        sa.Column("rules_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("applications", "rules_accepted_at")
