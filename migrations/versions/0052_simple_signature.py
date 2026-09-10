"""simple signature

Ruling #183 (docs/decisions.md; stage 10, track B1): a citizen acting for
THEMSELVES signs a document with a button, not an ERI certificate — an
applicant session exists only through OneID or E-IMZO login (decision #32,
no password), so the signer is already known by PINFL before any envelope
would ever be checked. `#9` required ERI of every applicant; this narrows
that to `on_behalf='legal'` only.

`signatures.kind` TEXT NOT NULL DEFAULT `'eri'`, CHECK `kind IN ('eri',
'simple')`. `certificate_id` becomes NULLABLE — a simple signature presents
no certificate at all — with a second CHECK tying the two together: `kind =
'eri'` always carries a `certificate_id`, `kind = 'simple'` never does. Every
row this migration finds already in the table is a real ERI signature, so
the column is added with `server_default='eri'` (dropped from nowhere —
`Signature.kind`'s own Python-side `default="eri"` is what the ORM model
declares, the same split `0051`'s `benefit_verification_status` uses: the
guard test comparing autogenerate to head does not compare server defaults).

Revision ID: 0052
Revises: 0051
Create Date: 2026-09-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0052"
down_revision: str | Sequence[str] | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "signatures",
        sa.Column("kind", sa.Text(), server_default="eri", nullable=False),
    )
    op.alter_column("signatures", "certificate_id", existing_type=sa.Uuid(), nullable=True)
    op.create_check_constraint("kind_valid", "signatures", "kind IN ('eri', 'simple')")
    op.create_check_constraint(
        "kind_certificate_pair",
        "signatures",
        "(kind = 'eri' AND certificate_id IS NOT NULL)"
        " OR (kind = 'simple' AND certificate_id IS NULL)",
    )


def downgrade() -> None:
    """Downgrade schema.

    Deletes every `kind = 'simple'` row FIRST — a simple signature carries no
    certificate by construction, so the plain `ALTER COLUMN ... SET NOT NULL`
    below would otherwise refuse the instant one real row exists in the table
    (lesson: a downgrade must delete whatever its upgrade made possible).
    `signatures` carries no append-only trigger (unlike `audit_log`/
    `calculations`/`application_status_history`) and nothing else FKs to
    `signatures.id` anywhere in this codebase, so a bare `DELETE` needs no
    `DISABLE TRIGGER`/`SAVEPOINT` dance — unlike the append-only cases this
    same lesson also covers.
    """
    op.execute(sa.text("DELETE FROM signatures WHERE kind = 'simple'"))
    op.drop_constraint("kind_certificate_pair", "signatures", type_="check")
    op.drop_constraint("kind_valid", "signatures", type_="check")
    op.alter_column("signatures", "certificate_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("signatures", "kind")
