"""inspection_case_templates

The four templates `inspections.service` notifies a violation case's own
violator on (ruling #138/R1, `docs/plans/07.6-handover-and-the-violator.md`;
finding F2, `07.5-audit-findings.md`): case opened, explanation requested,
decision taken, case closed. Modeled line for line on `0036_payment_reversed_
template.py`.

**Why this migration exists at all.** Before this stage `inspections` never
imported `notifications.service` at all — a citizen could be warned, asked to
explain within 5 working days, or have a permit suspended over their head,
and learn none of it from the platform, because nothing was ever SENT, not
merely untranslated. Seeding these four in the SAME commit as
`inspections.events.NOTIFIED_EVENT_CODES` gaining them is what
`test_every_event_this_module_notifies_on_has_a_template` requires — without
a template, `notify()` falls through to a raw, untranslated `inapp` row plus
a `notification.template_missing` ERROR log line, forever, on every
occurrence.

**Three languages, not two** (decision #90): `uz_latn` is the required
language in `LocalizedName` since `0032`'s backfill, `uz_cyrl` is optional
and kept because the printed side of the system still reads Cyrillic.

Both channels (`inapp`, `sms` — `notifications.service.DEFAULT_CHANNELS`)
carry the same sentence per event; nothing here is long enough to need a
shorter SMS variant the way `0020`'s split needed one.

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-07 12:00:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0038"
down_revision: str | Sequence[str] | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Dotted `notification_templates.event_code` values — `inspections.events`'s
# own constants, repeated here as literals because a migration is a frozen
# historical statement and must not change meaning if those constants are
# ever renamed (0025's own note).
CASE_OPENED = "violation_case.opened"
CASE_EXPLANATION_REQUESTED = "violation_case.explanation_requested"
CASE_DECIDED = "violation_case.decided"
CASE_CLOSED = "violation_case.closed"

# `{case_number}`, `{explanation_due_at}` and `{decision}` are exactly what
# `inspections.service._open_case`/`request_explanation`/`decide_case` pass;
# `notifications.service.render` substitutes them with its whitelist regex,
# never `str.format`.
BODIES: dict[str, dict[str, str]] = {
    CASE_OPENED: {
        "uz_latn": (
            "Sizga nisbatan {case_number} raqamli buzilish holati bo'yicha ish"
            " ochildi. Tafsilotlarni shaxsiy kabinetingizda ko'ring."
        ),
        "uz_cyrl": (
            "Сизга нисбатан {case_number} рақамли бузилиш ҳолати бўйича иш"
            " очилди. Тафсилотларни шахсий кабинетингизда кўринг."
        ),
        "ru": (
            "В отношении вас открыто дело о нарушении № {case_number}."
            " Подробности — в личном кабинете."
        ),
    },
    CASE_EXPLANATION_REQUESTED: {
        "uz_latn": (
            "{case_number}-son ish bo'yicha {explanation_due_at} sanasigacha"
            " tushuntirish xat taqdim eting."
        ),
        "uz_cyrl": (
            "{case_number}-сон иш бўйича {explanation_due_at} санасигача"
            " тушунтириш хат тақдим этинг."
        ),
        "ru": (
            "По делу № {case_number} необходимо представить объяснение до {explanation_due_at}."
        ),
    },
    CASE_DECIDED: {
        "uz_latn": (
            "{case_number}-son ish bo'yicha qaror qabul qilindi: {decision}."
            " Tafsilotlarni shaxsiy kabinetingizda ko'ring."
        ),
        "uz_cyrl": (
            "{case_number}-сон иш бўйича қарор қабул қилинди: {decision}."
            " Тафсилотларни шахсий кабинетингизда кўринг."
        ),
        "ru": (
            "По делу № {case_number} принято решение: {decision}. Подробности — в личном кабинете."
        ),
    },
    CASE_CLOSED: {
        "uz_latn": "{case_number}-son buzilish holati bo'yicha ish yopildi.",
        "uz_cyrl": "{case_number}-сон бузилиш ҳолати бўйича иш ёпилди.",
        "ru": "Дело о нарушении № {case_number} закрыто.",
    },
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
                "event_code": event_code,
                "channel": channel,
                "body": body,
                "version": 1,
                "status": "active",
            }
            for event_code, body in BODIES.items()
            for channel in CHANNELS
        ],
    )


def downgrade() -> None:
    """Downgrade schema."""
    # The notifications FIRST — `fk_notifications_template_id_notification_templates`
    # breaks the round-trip the moment anything has actually sent one of these
    # events (the 0010 trap; 0020's and 0036's downgrades carry the same two
    # statements in this order).
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
