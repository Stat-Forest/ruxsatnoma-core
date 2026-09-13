"""sms_applicant_only

Ruling #211 (docs/decisions.md): SMS goes to the applicant only, for three
events — the invoice (approval + amount + deadline), the rejection and the
permit coming into force — plus the OTP, which is not a template. Every
other `sms` template is ARCHIVED here, never deleted: 3.5 ruling 11 still
writes every notification to the cabinet, and 3.5 ruling 10 makes `notify()`
skip the SMS channel for an event with no active `sms` row, so no call site
changes. An admin can re-activate any archived text through the versioned
template table if the Agency asks for it back.

The three kept texts are SUPERSEDED (archive + insert a new version, never a
rewrite — 3.5 ruling 8) with the PM's wording. The application number already
carries its `RX-` series, so his «N{ariza_raqami}» is written without the
«N»; the placeholders are the ones the call sites already pass.

**Three languages** (decision #90). **The `sms` `uz_latn` bodies stay inside
GSM 03.38** (`tests/modules/notifications/test_sms_gsm_charset.py`: straight
ASCII apostrophes, hyphen-minus, no dash).

Revision ID: 0060
Revises: 0059
Create Date: 2026-09-13 18:00:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0060"
down_revision: str | Sequence[str] | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Dotted event codes repeated as literals — a migration is a frozen historical
# statement and must not change meaning if a constant is renamed (`0025`).
KEPT: dict[str, dict[str, str]] = {
    "invoice.issued": {
        "uz_latn": (
            "{application_number} arizangiz qabul qilindi."
            " {amount} so'm to'lovni {due_date} gacha amalga oshiring"
        ),
        "uz_cyrl": (
            "{application_number} аризангиз қабул қилинди."
            " {amount} сўм тўловни {due_date} гача амалга оширинг"
        ),
        "ru": ("Заявка {application_number} принята. Оплатите {amount} сум до {due_date}"),
    },
    "application.rejected": {
        "uz_latn": "{application_number} arizangiz rad etildi",
        "uz_cyrl": "{application_number} аризангиз рад этилди",
        "ru": "Заявка {application_number} отклонена",
    },
    "permit.active": {
        "uz_latn": "Ruxsatnoma {permit_number} berildi. Muddati: {valid_from}-{valid_to}",
        "uz_cyrl": "Рухсатнома {permit_number} берилди. Муддати: {valid_from}-{valid_to}",
        "ru": "Разрешение {permit_number} выдано. Срок: {valid_from}-{valid_to}",
    },
}


def _templates() -> sa.TableClause:
    return sa.table(
        "notification_templates",
        sa.column("id", sa.Uuid()),
        sa.column("event_code", sa.Text()),
        sa.column("channel", sa.Text()),
        sa.column("body", postgresql.JSONB(astext_type=sa.Text())),
        sa.column("version", sa.Integer()),
        sa.column("status", sa.Text()),
    )


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Every active `sms` text goes to the archive — the kept three included,
    #    because their new version is inserted right after (a supersede).
    op.execute(
        sa.text(
            "UPDATE notification_templates SET status = 'archived'"
            " WHERE channel = 'sms' AND status = 'active'"
        )
    )
    # 2. The three kept texts, one version above whatever exists for them.
    conn = op.get_bind()
    rows = []
    for event_code, body in KEPT.items():
        current = conn.execute(
            sa.text(
                "SELECT COALESCE(MAX(version), 0) FROM notification_templates"
                " WHERE event_code = :code AND channel = 'sms'"
            ).bindparams(code=event_code)
        ).scalar_one()
        rows.append(
            {
                # uuid4(), not app.db.uuid7 — a migration must not depend on
                # app code that could move or be renamed later (0009/0058).
                "id": uuid.uuid4(),
                "event_code": event_code,
                "channel": "sms",
                "body": body,
                "version": int(current) + 1,
                "status": "active",
            }
        )
    op.bulk_insert(_templates(), rows)


def downgrade() -> None:
    """Downgrade schema.

    Drops the three versions this migration inserted (their notifications
    first — the 0010 trap) and puts the highest remaining version of every
    `sms` template that has no active row back into force, which is exactly
    the set step 1 archived on a database that had never archived an `sms`
    text by hand.
    """
    for event_code in KEPT:
        op.execute(
            sa.text(
                "DELETE FROM notifications WHERE template_id IN ("
                " SELECT id FROM notification_templates"
                " WHERE event_code = :code AND channel = 'sms' AND status = 'active')"
            ).bindparams(code=event_code)
        )
        op.execute(
            sa.text(
                "DELETE FROM notification_templates"
                " WHERE event_code = :code AND channel = 'sms' AND status = 'active'"
            ).bindparams(code=event_code)
        )
    op.execute(
        sa.text(
            "UPDATE notification_templates t SET status = 'active'"
            " WHERE t.channel = 'sms' AND t.status = 'archived'"
            " AND t.version = (SELECT MAX(version) FROM notification_templates"
            "   WHERE event_code = t.event_code AND channel = 'sms')"
            " AND NOT EXISTS (SELECT 1 FROM notification_templates"
            "   WHERE event_code = t.event_code AND channel = 'sms' AND status = 'active')"
        )
    )
