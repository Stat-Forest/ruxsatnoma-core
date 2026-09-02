"""permit_due_template

The notification template `permits.subscribers.on_payment_confirmed` sends
(plan `03.11a-permits-core` ruling 19): money has arrived on an application and the
assigned hodim must now form the permit.

**Why this is not `payment.confirmed`.** `0009_notifications.py` already seeds that
code — «Оплата {amount} сум по заявке {application_number} подтверждена» — and that
sentence is a statement of fact addressed to the PAYER. It neither names what an
executor must do nor is true of them, and 3.10a will notify the applicant with it,
so reusing it would fire one sentence at two audiences from one event. Ruling 17's
point is that a template must exist for what is actually sent; a template that
exists and says the wrong thing is the same failure with a green test on top.

**The two channels carry different bodies**, unlike `0019`'s four templates. `inapp`
is the cabinet the hodim already works in and has no length limit, so it carries the
amount; a Cyrillic SMS bills at 70 characters per part (0009 ruling 20), so the SMS
body states the application and the action and stops. Both are addressed to a hodim
in the imperative — this is a task, not a receipt.

`0020` is this stage's second reserved revision number (`plans/03.9-3.11-parallel-run.md`);
`0017`/`0018` belong to the parallel 3.10a payments branch and are reconciled at merge
time with `alembic merge heads`, never by renumbering (lesson).

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-02 17:40:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Dotted `notification_templates.event_code`, never the flat bus name
# `payment_confirmed` that triggers it (`app/modules/permits/events.py` has the
# three-vocabularies table).
EVENT_CODE = "permit.due"

# (channel, body). `{application_number}` and `{amount}` are what
# `on_payment_confirmed` passes; `notifications.service.render` substitutes them
# with its whitelist regex, never `str.format`.
SEED_TEMPLATES: list[tuple[str, dict[str, str]]] = [
    (
        "inapp",
        {
            "uz_cyrl": (
                "{application_number} аризаси бўйича {amount} сўм тўлов тасдиқланди."
                " Рухсатномани расмийлаштиринг."
            ),
            "ru": (
                "По заявке {application_number} подтверждена оплата {amount} сум."
                " Оформите разрешение."
            ),
        },
    ),
    (
        "sms",
        {
            "uz_cyrl": "{application_number} аризаси тўланди. Рухсатномани расмийлаштиринг.",
            "ru": "Заявка {application_number} оплачена. Оформите разрешение.",
        },
    ),
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
                # uuid4(), not app.db.uuid7 — a migration must not depend on app code
                # that could move or be renamed later (0009 and 0019 made the same call).
                "id": uuid.uuid4(),
                "event_code": EVENT_CODE,
                "channel": channel,
                "body": body,
                "version": 1,
                "status": "active",
            }
            for channel, body in SEED_TEMPLATES
        ],
    )


def downgrade() -> None:
    """Downgrade schema."""
    # `notifications` rows are deleted BEFORE the templates they point at, or
    # `fk_notifications_template_id_notification_templates` breaks the round-trip the
    # moment anything has actually sent this event (the 0010 trap — lesson: a
    # downgrade must delete whatever its upgrade made possible).
    op.execute(
        sa.text("DELETE FROM notifications WHERE event_code = :code").bindparams(code=EVENT_CODE)
    )
    op.execute(
        sa.text("DELETE FROM notification_templates WHERE event_code = :code").bindparams(
            code=EVENT_CODE
        )
    )
