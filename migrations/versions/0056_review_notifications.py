"""review_notifications

Ruling #200 (docs/decisions.md): the two application transitions that told
nobody now tell the one person each concerns. SUBMITTED -> IN_REVIEW
(`applications.service.start_review`) notifies the APPLICANT that the file is
being looked at; PENDING_INFO -> IN_REVIEW (`respond_info`) notifies the STAFF
MEMBER who opened the request (`info_requests.requested_by`) that an answer
arrived — until now they learned it only from the worklist.

This migration seeds both codes for both default channels, the way `0009`
seeded `application.approved` — and like the approval, `start_review` then
passes `channels=("inapp",)`: "taken into work" is not worth a citizen's SMS
beside «подана» and the decision that follows (decision #152's reading). The
staff recipient of `info_responded` never receives SMS at all (decision #150).
The `sms` rows exist because `tests/modules/applications/test_end_to_end.py`
walks `applications.events.NOTIFIED_EVENT_CODES` against every default
channel — an unseeded `sms` template is the SILENT failure that guard exists
for, and a call-site restriction is invisible to it. Both codes are registered
in that tuple in this same commit.

**Three languages** (decision #90: `uz_latn` required, `uz_cyrl` optional).
`{application_number}` is exactly what both call sites pass beside the
`status_from`/`status_to` pair the inbox draws as chips (#198);
`notifications.service.render` substitutes it with its whitelist regex.

Revision ID: 0056
Revises: 0055
Create Date: 2026-09-10 22:00:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0056"
down_revision: str | Sequence[str] | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Dotted `notification_templates.event_code`s — `applications.service.
# NOTIFY_APPLICATION_REVIEW_STARTED` / `NOTIFY_APPLICATION_INFO_RESPONDED`,
# repeated here as literals because a migration is a frozen historical
# statement and must not change meaning if a constant is ever renamed
# (`0025`'s own note, repeated by `0036` and `0054`).
# The `sms` bodies stay inside GSM 03.38 (plain ASCII apostrophes, no dash —
# `tests/modules/notifications/test_sms_gsm_charset.py`) and one 70-character
# part with a real number substituted.
BODIES: dict[str, dict[str, dict[str, str]]] = {
    "application.review_started": {
        "inapp": {
            "uz_latn": "Ariza {application_number} ko'rib chiqishga qabul qilindi.",
            "uz_cyrl": "Ариза {application_number} кўриб чиқишга қабул қилинди.",
            "ru": "Заявка {application_number} принята в работу.",
        },
        "sms": {
            "uz_latn": "Ariza {application_number} ko'rib chiqishga qabul qilindi.",
            "uz_cyrl": "Ариза {application_number} кўриб чиқишга қабул қилинди.",
            "ru": "Заявка {application_number} принята в работу.",
        },
    },
    "application.info_responded": {
        "inapp": {
            "uz_latn": "{application_number} arizasi bo'yicha so'ralgan ma'lumotga javob keldi.",
            "uz_cyrl": "{application_number} аризаси бўйича сўралган маълумотга жавоб келди.",
            "ru": "По заявке {application_number} поступил ответ на запрос информации.",
        },
        "sms": {
            "uz_latn": "{application_number} arizasi bo'yicha javob keldi.",
            "uz_cyrl": "{application_number} аризаси бўйича жавоб келди.",
            "ru": "По заявке {application_number} поступил ответ.",
        },
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
                # app code that could move or be renamed later.
                "id": uuid.uuid4(),
                "event_code": event_code,
                "channel": channel,
                "body": body,
                "version": 1,
                "status": "active",
            }
            for event_code, channels in BODIES.items()
            for channel, body in channels.items()
        ],
    )


def downgrade() -> None:
    """Downgrade schema."""
    # The notifications FIRST — `fk_notifications_template_id_notification_
    # templates` breaks the round-trip the moment anything has actually sent
    # these events (the 0010 trap; 0020/0036/0054 carry this same pair).
    for event_code in BODIES:
        op.execute(
            sa.text("DELETE FROM notifications WHERE event_code = :code").bindparams(
                code=event_code
            )
        )
        op.execute(
            sa.text("DELETE FROM notification_templates WHERE event_code = :code").bindparams(
                code=event_code
            )
        )
