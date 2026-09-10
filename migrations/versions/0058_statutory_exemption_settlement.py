"""statutory_exemption_settlement

Ruling #202 (docs/decisions.md): the OTHER lawful zero. `science` has no
rate in VMQ 278 at all — `norms.calculator.calculate` prices it at exactly
zero with a `{"kind": "tariff", "reason": "no_tariff_by_law"}` breakdown
line, and only under the versioned `tariff_exempt:science` parameter
(migration `0013`). Ruling #185 taught `payments.service.issue_invoice` to
settle a zero-sum invoice ITSELF, but only one carrying a verified benefit
claim; a `science` invoice therefore stayed `pending` at 0 — unpayable
through Payme (`-31001` pins the amount) and unconfirmable by hand
(`ManualConfirmationIn.amount` is `gt=0`) — and the application sat
`INVOICED` forever, the hiding-shaped defect once more. With a FIXED-amount
receiver in the directory it did not even get that far: `ledger.
split_payment(0, ...)` refused ("configured shares total 15000.00 on a
payment of 0.00") and the head could not approve at all. That is the report
this migration's stage answers; the fix itself is a branch in
`issue_invoice` plus the zero case in `split_payment`.

This migration seeds ONLY the notification template the exemption branch
sends — `invoice.settled_by_law`, `inapp` and `sms`, the same two channels
`0054` seeded for `invoice.settled_by_benefit`, since the recipient is
always the applicant (decision #150). A separate template rather than
`0054`'s reused, because that one says «{benefit} imtiyozi qo'llanildi» —
"a benefit was applied" — and a scientist granted nothing was granted no
benefit: the law simply set no fee. Registered in `payments.events.
NOTIFIED_EVENT_CODES` in the same commit, as `tests/modules/payments/
test_expiry.py::test_every_event_this_module_notifies_on_has_a_template`
requires.

**Three languages** (decision #90). **The `sms` body stays inside GSM
03.38** (`tests/modules/notifications/test_sms_gsm_charset.py`: straight
ASCII apostrophes, no dash) and inside one 70-character part with the
longest seeded activity name substituted (`Ilmiy tadqiqot`, 14 characters).

Revision ID: 0058
Revises: 0057
Create Date: 2026-09-11 09:00:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0058"
down_revision: str | Sequence[str] | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Dotted `notification_templates.event_code` — `payments.events.
# INVOICE_SETTLED_BY_LAW`, repeated here as a literal because a migration is
# a frozen historical statement and must not change meaning if that constant
# is ever renamed (`0025`'s own note, repeated by `0036` and `0054`).
EVENT_CODE = "invoice.settled_by_law"

# `{invoice_number}` and `{activity}` are exactly what `service._settle_free`
# passes for a `StatutoryExemption`; `notifications.service.render`
# substitutes them with its whitelist regex, never `str.format`.
BODY: dict[str, dict[str, str]] = {
    "inapp": {
        "uz_latn": (
            "{invoice_number} hisobi bo'yicha to'lov talab qilinmaydi:"
            " {activity} uchun qonunda to'lov belgilanmagan."
        ),
        "uz_cyrl": (
            "{invoice_number} ҳисоби бўйича тўлов талаб қилинмайди:"
            " {activity} учун қонунда тўлов белгиланмаган."
        ),
        "ru": (
            "По счёту {invoice_number} оплата не требуется:"
            " для вида деятельности «{activity}» плата законом не установлена."
        ),
    },
    "sms": {
        # Plain ASCII apostrophes only (migration 0040's own rule) — one
        # character outside GSM 03.38 doubles the price of the whole message.
        # 58 characters with `Ilmiy tadqiqot` substituted, inside one part.
        "uz_latn": "To'lov talab qilinmaydi: {activity} uchun to'lov belgilanmagan.",
        "uz_cyrl": "Тўлов талаб қилинмайди: {activity} учун тўлов белгиланмаган.",
        "ru": "Оплата не требуется: для «{activity}» плата законом не установлена.",
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
                # 0025/0036/0054's own call).
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
    # this event (the 0010 trap; 0020's, 0036's and 0054's downgrades carry
    # this same pair, in this same order).
    op.execute(
        sa.text("DELETE FROM notifications WHERE event_code = :code").bindparams(code=EVENT_CODE)
    )
    op.execute(
        sa.text("DELETE FROM notification_templates WHERE event_code = :code").bindparams(
            code=EVENT_CODE
        )
    )
