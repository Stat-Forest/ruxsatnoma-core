"""activity seasons

Stage 9, ruling #177 (docs/decisions.md): season windows move from
«contour × activity» to «organization × activity» — a leshoz with 151
contours used to have to state its grazing season 151 times, and stated it
nowhere at all where no geobotanical survey exists and therefore no `Norm`
can be published. `activity_seasons` is that dictionary: `season` in the
exact JSONB shape `norms.season` already uses, plus `min_term_days`, unique
on (`organization_id`, `activity_type_id`). No lifecycle the way `norms`/
`tariffs`/`rule_parameters` have one — a plain current-value setting edited
in place.

`norms.seasons.manage` (new permission) is granted to `gis_specialist`
(zone-scoped — the leshoz edits its own row, `service._assert_organization_
zone`) and `central_admin` (zone-free — edits any), matching ruling #177's
own words: "editable by the leshoz for itself and by the central admin for
anyone". Role codes verified against `0003_auth.py`, never against plan
prose (lesson: a wrong code inserts zero rows, silently).

Revision ID: 0049
Revises: 0048
Create Date: 2026-09-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0049"
down_revision: str | Sequence[str] | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVITY_SEASONS_MANAGE_ROLES: tuple[str, ...] = ("gis_specialist", "central_admin")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "activity_seasons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("activity_type_id", sa.Uuid(), nullable=False),
        sa.Column("season", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("min_term_days", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "min_term_days IS NULL OR min_term_days > 0",
            name=op.f("ck_activity_seasons_min_term_days_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["activity_type_id"],
            ["activity_types.id"],
            name=op.f("fk_activity_seasons_activity_type_id_activity_types"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_activity_seasons_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_activity_seasons_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activity_seasons")),
        sa.UniqueConstraint(
            "organization_id", "activity_type_id", name="uq_activity_seasons_org_activity"
        ),
    )

    for role in ACTIVITY_SEASONS_MANAGE_ROLES:
        op.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_code) "
                "SELECT id, 'norms.seasons.manage' FROM roles WHERE code = :role "
                "ON CONFLICT DO NOTHING"
            ).bindparams(role=role)
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM role_permissions WHERE permission_code = 'norms.seasons.manage'")
    op.drop_table("activity_seasons")
