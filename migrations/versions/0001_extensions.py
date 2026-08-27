"""Расширения PostgreSQL (design/02, принцип 8)."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for ext in ("postgis", "btree_gist", "pg_trgm", "citext", "unaccent"):
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')


def downgrade() -> None:
    # Расширения не удаляем: их могут использовать другие объекты
    pass
