"""announcement_public_on_landing

The flag that lets an announcement out of the system and onto the public
`landing` site. Until now the only readers were authenticated: `GET
/announcements` resolves the caller's role and region and filters on
`audience`, so nothing here was ever reachable by a citizen with no session.

The flag is deliberately NOT "audience = everyone". An announcement carrying
an audience is addressed at staff (a role, a region), and `announcements_
service._reject_targeted_public` refuses to combine the two: the landing site
publishes only rows whose audience is empty, so a targeted notice cannot be
turned public by a single mis-click in the admin form.

`server_default` false, not nullable: every existing row predates the public
site and none of them was written to be read by the internet.

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-07 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "announcements",
        sa.Column(
            "public_on_landing", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    # The landing list is the one query that runs for anonymous traffic, so it gets
    # the only index: partial on the flag, since the public rows are a small subset.
    op.create_index(
        "ix_announcements_landing",
        "announcements",
        ["publish_from"],
        postgresql_where=sa.text("public_on_landing"),
    )


def downgrade() -> None:
    op.drop_index("ix_announcements_landing", table_name="announcements")
    op.drop_column("announcements", "public_on_landing")
