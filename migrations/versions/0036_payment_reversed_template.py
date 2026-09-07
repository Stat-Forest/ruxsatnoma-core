"""payment_reversed_template

The template `payments.service.record_reversal` notifies on (ruling #112,
stage 7.4d): Payme cancelled an already-performed transaction (state `2` ->
`-2`, reason `5`, "funds returned"), the invoice stays `paid` and a LIVE
permit is now standing on money that went back. RI-10 already records that
for the prosecutor's sweep; the notification is what reaches the one person
who may act on it — the leshoz's `executor_head`, `permits.manage`'s only
holder.

**Why this migration exists at all.** 7.4d deliberately shipped none, so its
`notify()` call fell through to `notifications.service`'s ruling-10 fallback:
a raw, untranslated `inapp` row plus a `notification.template_missing` ERROR
in the log, on every reversal, forever. A notification whose whole purpose is
that a human reads it cannot be the one that arrives untranslated — so this
seeds the real template and adds `payment.reversed` to
`payments.events.NOTIFIED_EVENT_CODES` in the same commit, which is what
`test_every_event_this_module_notifies_on_has_a_template` requires.

**Three languages, not two** (decision #90): `uz_latn` is the required
language in `LocalizedName` since `0032`'s backfill, `uz_cyrl` is optional and
kept because the printed side of the system still reads Cyrillic.

Both channels carry the same sentence. Unlike `0020`'s split, there is nothing
to drop for SMS here: the invoice number and the amount are the message, and a
reversal is rare enough that its SMS length is not a running cost.

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-07 09:00:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0036"
down_revision: str | Sequence[str] | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Dotted `notification_templates.event_code` — `payments.events.PAYMENT_REVERSED`,
# repeated here as a literal because a migration is a frozen historical statement
# and must not change meaning if that constant is ever renamed (0025's own note).
EVENT_CODE = "payment.reversed"

# `{invoice_number}`, `{reversed_amount}` and `{reason}` are exactly what
# `record_reversal` passes; `notifications.service.render` substitutes them with
# its whitelist regex, never `str.format`.
BODY: dict[str, str] = {
    "uz_latn": (
        "{invoice_number} hisobi boʻyicha {reversed_amount} soʻm toʻlov provayder"
        " tomonidan qaytarildi. Sababi: {reason}. Ruxsatnomani tekshiring."
    ),
    "uz_cyrl": (
        "{invoice_number} ҳисоби бўйича {reversed_amount} сўм тўлов провайдер"
        " томонидан қайтарилди. Сабаби: {reason}. Рухсатномани текширинг."
    ),
    "ru": (
        "По счёту {invoice_number} платёж {reversed_amount} сум возвращён"
        " провайдером. Причина: {reason}. Проверьте разрешение."
    ),
}

CHANNELS = ("inapp", "sms")


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
                # uuid4(), not app.db.uuid7 — a migration must not depend on app
                # code that could move or be renamed later (0009/0020/0025's call).
                "id": uuid.uuid4(),
                "event_code": EVENT_CODE,
                "channel": channel,
                "body": BODY,
                "version": 1,
                "status": "active",
            }
            for channel in CHANNELS
        ],
    )


def downgrade() -> None:
    """Downgrade schema."""
    # The notifications FIRST — `fk_notifications_template_id_notification_templates`
    # breaks the round-trip the moment anything has actually sent this event (the
    # 0010 trap; 0020's downgrade carries the same two statements in this order).
    op.execute(
        sa.text("DELETE FROM notifications WHERE event_code = :code").bindparams(code=EVENT_CODE)
    )
    op.execute(
        sa.text("DELETE FROM notification_templates WHERE event_code = :code").bindparams(
            code=EVENT_CODE
        )
    )
