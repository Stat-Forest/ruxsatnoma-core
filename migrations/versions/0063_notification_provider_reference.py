"""notification provider reference

Eskiz validates `user_sms_id` — the correlation id it echoes on every
delivery report — as a NUMBER of at most twelve digits: a live probe on
2026-09-14 had `999999999999` accepted and `1000000000000`, a uuid and a
uuid's hex all refused with `400 user_sms_id is invalid`. The 3.5 client sent
the notification's uuid, so every real send would have failed on the first
message; nothing noticed because `sms_mode` had never left `mock`.

`notifications.provider_reference` is that number: a `bigint` identity the
database hands out, so two rows never share one and the caller never shapes
it. Every row gets one (an `inapp` row simply never sends it) — a nullable
column filled only for `sms` would be a second code path for nothing. A
delivery report is correlated by it first, by `provider_message_id` second
(`notifications.service.apply_delivery_report`).

Revision ID: 0063
Revises: 0062
Create Date: 2026-09-14 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0063"
down_revision: str | Sequence[str] | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Postgres fills existing rows from the identity sequence as the column is
    # added, so the NOT NULL holds on a populated dev stand too.
    op.add_column(
        "notifications",
        sa.Column("provider_reference", sa.BigInteger(), sa.Identity(), nullable=False),
    )
    op.create_index(
        "uq_notifications_provider_reference", "notifications", ["provider_reference"], unique=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_notifications_provider_reference", table_name="notifications")
    op.drop_column("notifications", "provider_reference")
