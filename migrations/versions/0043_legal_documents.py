"""legal_documents

The register behind `landing`'s /documents page. Until now that page was four
rows hard-coded in the front-end — number, date, title, description — under a
"Download PDF" button with no href, no onClick and no files anywhere: it looked
like a service and was a picture of one.

Its own table rather than a flag on `announcements` (plan 07.8 ruling R1): a
news item has an audience and a publication window, an act has a number and a
date of adoption, and sharing one table would leave every row carrying the
other's dead columns.

The one index is partial on `status = 'published'` and ordered the way the
anonymous list reads the table (`sort_order`, then `adopted_on` descending):
that list is the only query internet traffic runs here, and the published rows
are a small subset of a table an editor appends to a few times a year.

Revision ID: 0043
Revises: merge_0039_0042
Create Date: 2026-09-08 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0043"
down_revision: str | Sequence[str] | None = "merge_0039_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "legal_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("doc_number", sa.Text(), nullable=False),
        sa.Column("adopted_on", sa.Date(), nullable=False),
        # Either of these two is enough to publish a row; neither is, and
        # `legal_documents_service.publish` is where that is enforced. The table
        # stays permissive on purpose: a draft is written before the PDF exists.
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('draft', 'published', 'archived')", name="status_valid"),
        sa.ForeignKeyConstraint(["file_id"], ["media_files.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
    )
    op.create_index(
        "ix_legal_documents_public",
        "legal_documents",
        ["sort_order", "adopted_on"],
        postgresql_where=sa.text("status = 'published'"),
    )


def downgrade() -> None:
    op.drop_index("ix_legal_documents_public", table_name="legal_documents")
    op.drop_table("legal_documents")
