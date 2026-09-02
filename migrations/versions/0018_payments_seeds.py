"""payments seeds

Seeds the `invoice.due_soon` notification template (plan `03.10a-payments-core`
task 6) — the one event code `payments.events.NOTIFIED_EVENT_CODES` gains that
migration `0009_notifications.py` could not have seeded, since it predates this
module. Mirrors `0009`'s own `_BODIES` shape: one dict of `{event_code: {uz_cyrl,
ru}}`, exploded into an (event_code, channel) row per channel in
`("inapp", "sms")`. No schema change — this migration is pure data, like
`0005_admin_seeds.py`/`0012_norms_seeds.py`.

The reminder shares its three placeholders (`application_number`, `amount`,
`due_date`) with `invoice.issued`'s own body: it is the same fact — an amount
due by a date for an application — read again before the window closes,
exactly what `payments.jobs.expiry_sweep`'s reminder pass passes as `params`.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-02 19:30:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_CODE = "invoice.due_soon"

_BODY: dict[str, str] = {
    "uz_cyrl": "{application_number} аризаси бўйича {amount} сўм тўлов муддати"
    " {due_date} санада тугайди.",
    "ru": "Срок оплаты {amount} сум по заявке {application_number} истекает {due_date}.",
}

SEED_TEMPLATES: list[tuple[str, str, dict[str, str]]] = [
    (EVENT_CODE, channel, _BODY) for channel in ("inapp", "sms")
]


def upgrade() -> None:
    """Upgrade schema."""
    templates = sa.table(
        "notification_templates",
        sa.column("id", sa.Uuid()),
        sa.column("event_code", sa.Text()),
        sa.column("channel", sa.Text()),
        sa.column("body", postgresql.JSONB(astext_type=sa.Text())),
        sa.column("version", sa.Integer()),
        sa.column("status", sa.Text()),
    )
    op.bulk_insert(
        templates,
        [
            {
                # Deliberately uuid4(), not app.db.uuid7 — migrations must not
                # depend on app code that could move/rename later (same call
                # as 0009/0010).
                "id": uuid.uuid4(),
                "event_code": event_code,
                "channel": channel,
                "body": body,
                "version": 1,
                "status": "active",
            }
            for event_code, channel, body in SEED_TEMPLATES
        ],
    )


def downgrade() -> None:
    """Downgrade schema."""
    # A downgrade must delete whatever its upgrade made possible (lesson,
    # mirroring 0010's own `gis.import.finished` seed): the notifications go
    # FIRST — once `payments.jobs.expiry_sweep` actually sends this event,
    # `notifications.template_id` references the row this migration seeds,
    # and deleting the template alone would break the round-trip on
    # `fk_notifications_template_id_notification_templates` the moment a
    # test sends a reminder.
    op.execute(sa.text("DELETE FROM notifications WHERE event_code = 'invoice.due_soon'"))
    op.execute(sa.text("DELETE FROM notification_templates WHERE event_code = 'invoice.due_soon'"))
