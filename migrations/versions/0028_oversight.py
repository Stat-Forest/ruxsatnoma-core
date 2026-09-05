"""oversight

Plan `04.2-4.4-oversight-dashboard`. Two new tables (design/02 § oversight):
`oversight_events` (the accumulating stream for RN) and `risk_indicators`
(RI-01..15). `dashboard` (4.4, the other half of this track) adds no table of
its own (design/02: "queries over the other tables").

`risk_indicators.code` admits the full 15-code catalogue even though only a
subset has a working detector as of this migration (see `oversight/service.py`
for which) — so a future detector never needs a migration just to store its
own code.

This migration touches ONLY tables this module owns. `oversight.service.
harvest` scans the EXISTING `audit_log` table (owned by `audit`, level 0) for
rows several modules already tag with `extra = {"risk_indicator": "RI-xx"}`
(grepped and catalogued in the plan) — a plain `WHERE extra ? 'risk_indicator'
AND NOT EXISTS (...)` sequential scan, no index added to `audit_log`. At
today's volumes this is cheap; a partial index there would need its own ORM
mirror in `audit.models.AuditLog` (the autogenerate-diff guard), which is a
cross-module reach a level-5 reader should not make on its own — left as a
follow-up for whoever owns `audit` once volumes justify it (see the plan).

Revision ID: 0028
Revises: 0023
Create Date: 2026-09-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Role codes verified against `0003_auth.py`, never from plan/tz prose (the
# lesson on «Раҳбар» silently inserting zero rows). `tz/03`'s matrix gives the
# `Dashboard` row to every staff role except `applicant`; `oversight.view`
# (the RI/oversight-events surface, not a raw table) goes to the roles the
# same matrix already gives audit-log-adjacent visibility to.
DASHBOARD_VIEW_ROLES: tuple[str, ...] = (
    "central_admin",
    "leadership",
    "executor_staff",
    "gis_specialist",
    "executor_head",
    "inspector",
    "accountant",
    "prosecutor",
)
OVERSIGHT_VIEW_ROLES: tuple[str, ...] = (
    "central_admin",
    "leadership",
    "executor_head",
    "prosecutor",
)
ROLE_GRANTS: list[tuple[str, str]] = [
    *((role, "dashboard.view") for role in DASHBOARD_VIEW_ROLES),
    *((role, "oversight.view") for role in OVERSIGHT_VIEW_ROLES),
]
PERMISSION_CODES: tuple[str, ...] = ("dashboard.view", "oversight.view")


def upgrade() -> None:
    op.create_table(
        "oversight_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=True),
        sa.Column("object_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("rn_status", sa.Text(), nullable=False),
        sa.Column("rn_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "rn_status IN ('internal', 'pending', 'sent', 'failed')",
            name=op.f("ck_oversight_events_rn_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oversight_events")),
    )
    op.create_index(
        "ix_oversight_events_object",
        "oversight_events",
        ["object_type", "object_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_oversight_events_occurred_at_brin",
        "oversight_events",
        ["occurred_at"],
        unique=False,
        postgresql_using="brin",
    )
    op.create_table(
        "risk_indicators",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=True),
        sa.Column("object_id", sa.Uuid(), nullable=True),
        sa.Column("responsible_user_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rn_status", sa.Text(), nullable=False),
        sa.Column("rn_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "code IN ('RI-01', 'RI-02', 'RI-03', 'RI-04', 'RI-05', 'RI-06', 'RI-07', "
            "'RI-08', 'RI-09', 'RI-10', 'RI-11', 'RI-12', 'RI-13', 'RI-14', 'RI-15')",
            name=op.f("ck_risk_indicators_code_valid"),
        ),
        sa.CheckConstraint(
            "level IN ('low', 'medium', 'high', 'critical')",
            name=op.f("ck_risk_indicators_level_valid"),
        ),
        sa.CheckConstraint(
            "rn_status IN ('internal', 'pending', 'sent', 'failed')",
            name=op.f("ck_risk_indicators_rn_status_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('new', 'in_review', 'closed')",
            name=op.f("ck_risk_indicators_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["responsible_user_id"],
            ["users.id"],
            name=op.f("fk_risk_indicators_responsible_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_indicators")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_risk_indicators_idempotency_key")),
    )
    op.create_index(
        "ix_risk_indicators_code_occurred_at",
        "risk_indicators",
        ["code", "occurred_at"],
        unique=False,
    )

    for role, code in ROLE_GRANTS:
        op.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_code) "
                "SELECT id, :code FROM roles WHERE code = :role "
                "ON CONFLICT DO NOTHING"
            ).bindparams(code=code, role=role)
        )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_code = ANY(:codes)").bindparams(
            codes=list(PERMISSION_CODES)
        )
    )
    op.drop_index("ix_risk_indicators_code_occurred_at", table_name="risk_indicators")
    op.drop_table("risk_indicators")
    op.drop_index(
        "ix_oversight_events_occurred_at_brin",
        table_name="oversight_events",
        postgresql_using="brin",
    )
    op.drop_index("ix_oversight_events_object", table_name="oversight_events")
    op.drop_table("oversight_events")
