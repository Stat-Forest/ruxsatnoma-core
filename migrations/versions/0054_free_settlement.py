"""free_settlement

Stage 10, ruling #185 (docs/decisions.md; wave-1 track B4): every #181
benefit prices its claim at exactly zero, so `payments.service.issue_invoice`
settles a zero-sum invoice carrying one of those claims ITSELF
(`_settle_free`) rather than issuing a `pending` invoice nobody could ever
confirm (`ManualConfirmationIn.amount` is bounded `gt=0`, Payme's own
`-31001` pins `transaction.amount == invoice.amount`) — the exact
hiding-shaped defect this project keeps finding, left open, would strand the
application `INVOICED` forever.

This migration seeds ONLY the notification template `_settle_free` sends
instead of the ordinary "please pay" one — `invoice.settled_by_benefit`,
`inapp` and `sms`, both channels `0036`'s `payment.reversed` template
seeded, since the recipient is always the applicant (decision #150: `sms` is
closed to every role but `applicant`, exactly who this event is ever raised
for). It touches no column and no table: the whole feature is a branch in
`issue_invoice` plus this seeded text, registered in `payments.events.
NOTIFIED_EVENT_CODES` in the same commit — required by `tests/modules/
payments/test_expiry.py::test_every_event_this_module_notifies_on_has_a_
template`, which walks that tuple against exactly what this migration seeds.

**Three languages** (decision #90: `uz_latn` is the required language in
`LocalizedName`, `uz_cyrl` optional). **The `sms` body stays inside GSM
03.38** (`tests/modules/notifications/test_sms_gsm_charset.py` — a straight
ASCII apostrophe, never `oʻ`'s U+02BB, and no dash character at all) and
inside one 70-character part even for the longest seeded benefit code
(`persons_with_disabilities`, decision #181's own list) — a citizen told
"nothing to pay" must not be billed 70 tiyin twice for hearing it.

Revision ID: 0054
Revises: 0052
Create Date: 2026-09-10 12:00:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0054"
down_revision: str | Sequence[str] | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Dotted `notification_templates.event_code` — `payments.events.
# INVOICE_SETTLED_BY_BENEFIT`, repeated here as a literal because a migration
# is a frozen historical statement and must not change meaning if that
# constant is ever renamed (`0025`'s own note, repeated by `0036`).
EVENT_CODE = "invoice.settled_by_benefit"

# `{invoice_number}` and `{benefit}` are exactly what `service._settle_free`
# passes; `notifications.service.render` substitutes them with its whitelist
# regex, never `str.format` (a template is admin-authored text).
BODY: dict[str, dict[str, str]] = {
    "inapp": {
        "uz_latn": (
            "{invoice_number} hisobi bo'yicha to'lov talab qilinmaydi:"
            " {benefit} imtiyozi qo'llanildi."
        ),
        "uz_cyrl": (
            "{invoice_number} ҳисоби бўйича тўлов талаб қилинмайди: {benefit} имтиёзи қўлланилди."
        ),
        "ru": "По счёту {invoice_number} оплата не требуется: применена льгота «{benefit}».",
    },
    "sms": {
        # Plain ASCII apostrophes only (migration 0040's own rule) — one
        # character outside GSM 03.38 doubles the price of the whole message.
        # 60 characters even at the longest seeded benefit code
        # (`persons_with_disabilities`, decision #181), well inside one part.
        "uz_latn": "To'lov talab qilinmaydi: {benefit} imtiyozi.",
        "uz_cyrl": "Тўлов талаб қилинмайди: {benefit} имтиёзи.",
        "ru": "Оплата не требуется: льгота «{benefit}».",
    },
}


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
                # uuid4(), not app.db.uuid7 — a migration must not depend on
                # app code that could move or be renamed later (0009/0020/
                # 0025/0036's own call).
                "id": uuid.uuid4(),
                "event_code": EVENT_CODE,
                "channel": channel,
                "body": body,
                "version": 1,
                "status": "active",
            }
            for channel, body in BODY.items()
        ],
    )


def downgrade() -> None:
    """Downgrade schema."""
    # The notifications FIRST — `fk_notifications_template_id_notification_
    # templates` breaks the round-trip the moment anything has actually sent
    # this event (the 0010 trap; 0020's and 0036's downgrades carry this same
    # pair, in this same order).
    op.execute(
        sa.text("DELETE FROM notifications WHERE event_code = :code").bindparams(code=EVENT_CODE)
    )
    op.execute(
        sa.text("DELETE FROM notification_templates WHERE event_code = :code").bindparams(
            code=EVENT_CODE
        )
    )
