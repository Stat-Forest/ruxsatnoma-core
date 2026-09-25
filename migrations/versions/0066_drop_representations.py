"""drop representations

Stage 18 task B4 (decision #226): an organisation now logs in with its own
E-IMZO key straight into `applicants.owner_user_id` — the "representation"
mechanism (a person attaching a legal entity to their personal profile via
org ERI / OneID directors' registry / PDF power of attorney) is removed
everywhere. R7: the dev stand holds test data only, so this drops
`applications.representation_id` and the `representations` table outright,
no data migration. `applications.on_behalf` and its CHECK are left alone —
still `('self', 'legal')` — so a historical `'legal'` row filed before this
stage still reads back honestly; only `representation_id`, the record of
which power of attorney a filing was made under, is meaningless now that
nothing writes it.

Revision ID: 0066
Revises: 0064
Create Date: 2026-09-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0066"
down_revision: str | Sequence[str] | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index(op.f("ix_applications_representation_id"), table_name="applications")
    op.drop_constraint(
        op.f("fk_applications_representation_id_representations"),
        "applications",
        type_="foreignkey",
    )
    op.drop_column("applications", "representation_id")

    op.drop_index(
        "uq_representations_active",
        table_name="representations",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_index("ix_representations_user", table_name="representations")
    op.drop_table("representations")


def downgrade() -> None:
    """Downgrade schema — recreates both empty, exactly as `0006_applicants.py`
    first created `representations` and `0015_applications.py` first added
    `applications.representation_id` — INCLUDING the `poa_file_id` FK that
    `0007_admin_users.py` added afterwards (`NOT VALID` + `VALIDATE
    CONSTRAINT`, deferred there because `media_files` did not exist yet at
    0006). The round-trip test walks all the way back to `base`, so 0007's
    OWN downgrade tries to drop `fk_representations_poa_file_id_media_files`
    next — recreating the table without it here left nothing for 0007 to
    drop (`UndefinedObjectError`)."""
    op.create_table(
        "representations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("applicant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column("poa_file_id", sa.Uuid(), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "basis <> 'poa' OR (poa_file_id IS NOT NULL AND valid_until IS NOT NULL)",
            name=op.f("ck_representations_poa_requires_file_and_term"),
        ),
        sa.CheckConstraint(
            "basis IN ('director_registry', 'poa', 'org_eri')",
            name=op.f("ck_representations_basis_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'revoked')",
            name=op.f("ck_representations_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["applicant_id"],
            ["applicants.id"],
            name=op.f("fk_representations_applicant_id_applicants"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_representations_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_representations")),
    )
    op.create_foreign_key(
        op.f("fk_representations_poa_file_id_media_files"),
        "representations",
        "media_files",
        ["poa_file_id"],
        ["id"],
    )
    op.create_index("ix_representations_user", "representations", ["user_id"], unique=False)
    op.create_index(
        "uq_representations_active",
        "representations",
        ["applicant_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.add_column("applications", sa.Column("representation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_applications_representation_id_representations"),
        "applications",
        "representations",
        ["representation_id"],
        ["id"],
    )
    op.create_index(
        op.f("ix_applications_representation_id"),
        "applications",
        ["representation_id"],
        unique=False,
    )
