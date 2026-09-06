"""public and help

Schema for stage 4.6 `public` + 4.8 `help` (`design/02` §§ public, help — corrected
by plan `04.6-4.8-public-help.md`). Four tables, none of them shared with any other
module this stage: `citizen_appeals` (obrashcheniya, anonymous by design — R3/R5 in
the plan), `faq_items`, `support_tickets` and `support_ticket_messages`.

`qr_check_log`, filed under `## public` in `design/02`, is NOT created here — it
already exists (migration `0019`), owned and written by `permits` (3.11a ruling 15).

`down_revision` is `0023`, this branch's fork point (`04-06-parallel-run.md`): every
backend track in this fleet forked from `0023` at once, so whichever track merges
first keeps this parent and every other one re-points its own `down_revision` onto
the merged head before opening its PR — this branch is not merging, so that
re-pointing is the next session's job, not this commit's.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0030"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "faq_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("question", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("answer", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
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
            "status IN ('draft', 'published', 'archived')", name=op.f("ck_faq_items_status_valid")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_faq_items")),
    )
    op.create_index("ix_faq_items_status_sort", "faq_items", ["status", "sort_order"])

    op.create_table(
        "support_tickets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("assigned_to", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL)",
            name=op.f("ck_support_tickets_closed_at_consistent"),
        ),
        sa.CheckConstraint(
            "status IN ('new', 'in_progress', 'resolved', 'closed')",
            name=op.f("ck_support_tickets_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to"], ["users.id"], name=op.f("fk_support_tickets_assigned_to_users")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_support_tickets_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_tickets")),
        sa.UniqueConstraint("number", name=op.f("uq_support_tickets_number")),
    )
    op.create_index(op.f("ix_support_tickets_user_id"), "support_tickets", ["user_id"])

    op.create_table(
        "citizen_appeals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("applicant_name", sa.Text(), nullable=False),
        sa.Column("contact", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("answered_by", sa.Uuid(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'answered') = (answer_text IS NOT NULL AND answered_at IS NOT NULL)",
            name=op.f("ck_citizen_appeals_answered_fields_consistent"),
        ),
        sa.CheckConstraint(
            "status IN ('new', 'in_progress', 'answered', 'closed')",
            name=op.f("ck_citizen_appeals_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["answered_by"], ["users.id"], name=op.f("fk_citizen_appeals_answered_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["file_id"], ["media_files.id"], name=op.f("fk_citizen_appeals_file_id_media_files")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_citizen_appeals")),
        sa.UniqueConstraint("number", name=op.f("uq_citizen_appeals_number")),
    )
    op.create_index(op.f("ix_citizen_appeals_file_id"), "citizen_appeals", ["file_id"])
    op.create_index("ix_citizen_appeals_status", "citizen_appeals", ["status"])

    op.create_table(
        "support_ticket_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name=op.f("fk_support_ticket_messages_author_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["media_files.id"],
            name=op.f("fk_support_ticket_messages_file_id_media_files"),
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["support_tickets.id"],
            name=op.f("fk_support_ticket_messages_ticket_id_support_tickets"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_ticket_messages")),
    )
    op.create_index(
        op.f("ix_support_ticket_messages_file_id"), "support_ticket_messages", ["file_id"]
    )
    op.create_index(
        op.f("ix_support_ticket_messages_ticket_id"), "support_ticket_messages", ["ticket_id"]
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_support_ticket_messages_ticket_id"), table_name="support_ticket_messages"
    )
    op.drop_index(op.f("ix_support_ticket_messages_file_id"), table_name="support_ticket_messages")
    op.drop_table("support_ticket_messages")

    op.drop_index("ix_citizen_appeals_status", table_name="citizen_appeals")
    op.drop_index(op.f("ix_citizen_appeals_file_id"), table_name="citizen_appeals")
    op.drop_table("citizen_appeals")

    op.drop_index(op.f("ix_support_tickets_user_id"), table_name="support_tickets")
    op.drop_table("support_tickets")

    op.drop_index("ix_faq_items_status_sort", table_name="faq_items")
    op.drop_table("faq_items")
